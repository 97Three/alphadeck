import hashlib
import logging
import math
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime

from .integration_config import integration_scope, memory_config
from .memory_http import MemoryHttp
from .memory_policy import is_preference, personalization_key, preference_context, select_projection
from .memory_repository import PROVIDER, MemoryRepository
from .messages import js_length, js_slice

CAPABILITIES = [
    "preference.write",
    "preference.list",
    "preference.correct",
    "preference.history",
    "recall.trace",
    "recall.bitemporal",
    "recall.context-projection",
    "experience.usage-receipt",
    "experience.outcome",
]
CHAT_CAPABILITIES = CAPABILITIES[4:8]


class MemoryIntegrationError(Exception):
    def __init__(self, code, status, message):
        self.code, self.status = code, status
        super().__init__(message)


def bounded_text(value, label, maximum):
    value = value.strip()
    if not value or js_length(value) > maximum:
        raise MemoryIntegrationError("INVALID_MEMORY_INPUT", 400, "Invalid " + label + ".")
    return value


def subject_id(value):
    value = value.strip()
    if not value or js_length(value) > 128:
        raise MemoryIntegrationError("INVALID_MEMORY_SUBJECT", 400, "Invalid memory subject.")
    return value


def event_id(value):
    value = value.strip()
    if not value or js_length(value) > 160 or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        raise MemoryIntegrationError("INVALID_EVENT_ID", 400, "Invalid client event ID.")
    return value


def compatibility_issues(info, config, capabilities):
    return ["capability:" + item for item in capabilities if item not in info["capabilities"]] + (
        ["production:not-ready", *["blocker:" + blocker for blocker in info["productionBlockers"]]]
        if config["requireProductionReady"] and not info["productionReady"]
        else []
    )


class MemoryService:
    def __init__(
        self, database, *, config=None, port=None, repository=None, scope_factory=integration_scope
    ):
        self.config = config if config is not None else memory_config()
        self.port, self.repository, self.scope = (
            port,
            repository or MemoryRepository(database),
            scope_factory,
        )

    @asynccontextmanager
    async def connection(self):
        if self.port:
            yield self.port
        else:
            async with MemoryHttp(self.config) as port:
                yield port

    def require_enabled(self):
        if not self.config["enabled"]:
            raise MemoryIntegrationError(
                "MEMORY_DISABLED", 503, "Personal memory integration is disabled."
            )

    async def enabled_control(self, actor):
        control = await self.repository.control(actor)
        if not control["personalizationEnabled"]:
            raise MemoryIntegrationError(
                "MEMORY_OPTED_OUT", 409, "Personal memory is disabled for this account."
            )
        return control

    async def negotiate(self, port, capabilities, request_id=None):
        info = await port.discover(request_id)
        issues = compatibility_issues(info, self.config, capabilities)
        if issues:
            raise MemoryIntegrationError(
                "MEMORY_INCOMPATIBLE",
                503,
                "Personal memory service is incompatible: " + ", ".join(issues) + ".",
            )
        return info

    def provider_scope(self, actor):
        return {
            "tenantId": self.config["tenantId"],
            "subjectId": actor,
            "purpose": self.config["purpose"],
        }

    async def inspect(self, request_id=None):
        self.require_enabled()
        async with self.connection() as port:
            info = await self.negotiate(port, CAPABILITIES, request_id)
            await port.check_ready(request_id)
            return info

    async def recall(self, project, actor, request, instruction, capability=None):
        def empty(status):
            return {"status": status, "capsule": None, "exposedMemoryCount": 0, "preparedUse": None}

        if not self.config["enabled"]:
            return empty("disabled")
        actor = subject_id(actor)
        try:
            if not (await self.repository.control(actor))["personalizationEnabled"]:
                return empty("opted_out")
            async with self.connection() as port:
                await self.negotiate(port, CHAT_CAPABILITIES, request)
                recall = await port.recall(
                    {
                        **self.provider_scope(actor),
                        "query": js_slice(instruction.strip(), 0, 4096),
                        "context": {
                            "product": "alphadeck",
                            "project_id": project,
                            **({"capability": js_slice(capability, 0, 512)} if capability else {}),
                        },
                        "limit": self.config["recallLimit"],
                    },
                    request,
                )
                projection = await port.project_context(
                    {
                        **self.provider_scope(actor),
                        "traceId": recall["traceId"],
                        "algorithm": "exact-deduplicated-v1",
                        "maxCharacters": self.config["maxProjectionCharacters"],
                    },
                    request,
                )
            selected = select_projection(projection, project)
            if not (await self.repository.control(actor))["personalizationEnabled"]:
                return empty("opted_out")
            if not selected["revisionIds"]:
                return empty("empty")
            digest = hashlib.sha256(selected["content"].encode()).hexdigest()
            return {
                "status": "prepared",
                "exposedMemoryCount": len(selected["revisionIds"]),
                "capsule": {
                    "content": selected["content"],
                    "traceId": recall["traceId"],
                    "revisionIds": selected["revisionIds"],
                    "sourceProjectionSha256": projection["projectionSha256"],
                    "contentSha256": digest,
                },
                "preparedUse": {
                    "tenantId": self.config["tenantId"],
                    "subjectId": actor,
                    "traceId": recall["traceId"],
                    "policyId": recall["policyId"],
                    "policyVersion": recall["policyVersion"],
                    "validAt": recall["validAt"],
                    "knownAt": recall["knownAt"],
                    "algorithm": "exact-deduplicated-v1",
                    "maxCharacters": self.config["maxProjectionCharacters"],
                    "sourceProjectionSha256": projection["projectionSha256"],
                    "deliveredContextSha256": digest,
                    "exposedRevisionIds": selected["revisionIds"],
                },
            }
        except Exception as cause:
            if self.config["required"]:
                raise MemoryIntegrationError(
                    "MEMORY_REQUIRED_UNAVAILABLE", 503, "Required personal memory is unavailable."
                ) from cause
            logging.warning(
                "Optional personal memory unavailable (%s)",
                getattr(cause, "code", "INTEGRATION_ERROR"),
            )
            return empty("unavailable")

    async def expose(self, project, actor, request, recall):
        if (
            recall.get("status") != "prepared"
            or not recall.get("capsule")
            or not recall.get("preparedUse")
            or not self.config["enabled"]
        ):
            return None
        actor, request = subject_id(actor), event_id(request)
        prepared, capsule = recall["preparedUse"], recall["capsule"]
        try:
            valid, known = [
                datetime.fromisoformat(prepared[key].replace("Z", "+00:00"))
                for key in ("validAt", "knownAt")
            ]
            matches = (
                prepared["tenantId"] == self.config["tenantId"]
                and prepared["subjectId"] == actor
                and prepared["traceId"] == capsule["traceId"]
                and prepared["sourceProjectionSha256"] == capsule["sourceProjectionSha256"]
                and prepared["deliveredContextSha256"] == capsule["contentSha256"]
                and prepared["exposedRevisionIds"] == capsule["revisionIds"]
            )
        except (ValueError, KeyError, TypeError):
            matches = False
        if not matches:
            raise MemoryIntegrationError(
                "MEMORY_USE_MISMATCH",
                400,
                "Prepared personal memory attribution does not match the delivered capsule.",
            )
        try:
            if not (await self.repository.control(actor))["personalizationEnabled"]:
                return None
            async with self.connection() as port:
                usage = await port.record_usage(
                    {
                        "tenantId": prepared["tenantId"],
                        "subjectId": prepared["subjectId"],
                        "traceId": prepared["traceId"],
                        "algorithm": prepared["algorithm"],
                        "maxCharacters": prepared["maxCharacters"],
                        "sourceProjectionSha256": prepared["sourceProjectionSha256"],
                        "deliveredContextSha256": prepared["deliveredContextSha256"],
                        "revisionIds": prepared["exposedRevisionIds"],
                        "idempotencyKey": f"alphadeck:{request}:memory-usage",
                        "purpose": self.config["purpose"],
                    },
                    request,
                )
            if any(
                usage[key] != prepared[source]
                for key, source in (
                    ("traceId", "traceId"),
                    ("algorithm", "algorithm"),
                    ("maxCharacters", "maxCharacters"),
                    ("sourceProjectionSha256", "sourceProjectionSha256"),
                    ("deliveredContextSha256", "deliveredContextSha256"),
                    ("revisionIds", "exposedRevisionIds"),
                )
            ):
                raise MemoryIntegrationError(
                    "MEMORY_USAGE_MISMATCH",
                    502,
                    "Personal memory usage receipt does not match the delivered capsule.",
                )
            await self.repository.save_use(
                {
                    "provider": PROVIDER,
                    "projectId": project,
                    "requestId": request,
                    "tenantId": prepared["tenantId"],
                    "integrationScopeSha256": self.scope(project)["scopeSha256"],
                    "subjectId": actor,
                    "traceId": prepared["traceId"],
                    "providerUsageId": usage["usageId"],
                    "policyId": prepared["policyId"],
                    "policyVersion": prepared["policyVersion"],
                    "validAt": valid,
                    "knownAt": known,
                    "sourceProjectionSha256": prepared["sourceProjectionSha256"],
                    "deliveredContextSha256": prepared["deliveredContextSha256"],
                    "exposedRevisionIds": prepared["exposedRevisionIds"],
                }
            )
            return {**capsule, "usageId": usage["usageId"]}
        except Exception as cause:
            if self.config["required"]:
                raise MemoryIntegrationError(
                    "MEMORY_REQUIRED_UNAVAILABLE",
                    503,
                    "Required personal memory attribution is unavailable.",
                ) from cause
            logging.warning(
                "Prepared memory attribution failed (%s)",
                getattr(cause, "code", "INTEGRATION_ERROR"),
            )
            return None

    async def list_preferences(self, actor, request_id=None):
        self.require_enabled()
        actor = subject_id(actor)
        async with self.connection() as port:
            await self.negotiate(port, ["preference.list"], request_id)
            return [
                value
                for value in await port.list_preferences(self.provider_scope(actor), request_id)
                if is_preference(value)
            ]

    async def remember(
        self,
        project,
        actor,
        event,
        key,
        value,
        evidence,
        *,
        confidence=0.95,
        scope="global",
        context=None,
        occurred_at=None,
    ):
        self.require_enabled()
        if (
            type(confidence) not in {int, float}
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise MemoryIntegrationError("INVALID_MEMORY_INPUT", 400, "Invalid confidence.")
        event, actor = event_id(event), subject_id(actor)
        try:
            key = personalization_key(key)
        except ValueError as cause:
            raise MemoryIntegrationError(
                "INVALID_MEMORY_KEY", 400, "Unsupported personal memory key."
            ) from cause
        value, evidence = (
            bounded_text(value, "preference value", 4096),
            bounded_text(evidence, "evidence text", 16384),
        )
        context = preference_context(project, scope, context)
        await self.enabled_control(actor)
        async with self.connection() as port:
            await self.negotiate(port, ["preference.write"], event)
            return await port.remember_preference(
                {
                    **self.provider_scope(actor),
                    "source": "alphadeck-explicit-confirmation",
                    "idempotencyKey": f"alphadeck:{event}:preference",
                    "key": key,
                    "value": value,
                    "context": context,
                    "evidenceText": evidence,
                    "confidence": confidence,
                    **({"occurredAt": occurred_at} if occurred_at else {}),
                },
                event,
            )

    async def correct(
        self,
        actor,
        record,
        event,
        value,
        evidence,
        reason,
        *,
        expected_revision=None,
        occurred_at=None,
    ):
        self.require_enabled()
        event, actor = event_id(event), subject_id(actor)
        record = bounded_text(record, "record ID", 64)
        value, evidence, reason = (
            bounded_text(value, "preference value", 4096),
            bounded_text(evidence, "evidence text", 16384),
            bounded_text(reason, "correction reason", 2048),
        )
        await self.enabled_control(actor)
        async with self.connection() as port:
            await self.negotiate(port, ["preference.list", "preference.correct"], event)
            visible = await port.list_preferences(self.provider_scope(actor), event)
            if not any(item["recordId"] == record and is_preference(item) for item in visible):
                raise MemoryIntegrationError(
                    "MEMORY_PREFERENCE_NOT_FOUND", 404, "Memory preference was not found."
                )
            return await port.correct_preference(
                {
                    **self.provider_scope(actor),
                    "recordId": record,
                    "source": "alphadeck-explicit-correction",
                    "idempotencyKey": f"alphadeck:{event}:correction",
                    "value": value,
                    "evidenceText": evidence,
                    "reason": reason,
                    **(
                        {"expectedRevisionId": expected_revision}
                        if expected_revision is not None
                        else {}
                    ),
                    **({"occurredAt": occurred_at} if occurred_at else {}),
                },
                event,
            )

    async def revisions(self, actor, record, request_id=None):
        self.require_enabled()
        actor, record = subject_id(actor), bounded_text(record, "record ID", 64)
        async with self.connection() as port:
            await self.negotiate(port, ["preference.list", "preference.history"], request_id)
            visible = await port.list_preferences(self.provider_scope(actor), request_id)
            if not any(item["recordId"] == record and is_preference(item) for item in visible):
                raise MemoryIntegrationError(
                    "MEMORY_PREFERENCE_NOT_FOUND", 404, "Memory preference was not found."
                )
            return await port.get_revisions(
                {**self.provider_scope(actor), "recordId": record}, request_id
            )

    async def use_attribution(self, project, actor, request):
        self.require_enabled()
        use = await self.repository.find_use(project, request)
        if (
            not use
            or use["tenantId"] != self.config["tenantId"]
            or use["subjectId"] != subject_id(actor)
        ):
            raise MemoryIntegrationError("MEMORY_USE_NOT_FOUND", 404, "Memory use was not found.")
        return {
            "requestId": use["requestId"],
            "revisionIds": use["exposedRevisionIds"],
            "contentSha256": use["deliveredContextSha256"],
        }

    async def feedback(
        self,
        project,
        actor,
        request,
        revision,
        event,
        kind,
        *,
        weight=1,
        note=None,
        occurred_at=None,
    ):
        self.require_enabled()
        actor = subject_id(actor)
        use = await self.repository.find_use(project, request)
        if not use or use["tenantId"] != self.config["tenantId"] or use["subjectId"] != actor:
            raise MemoryIntegrationError("MEMORY_USE_NOT_FOUND", 404, "Memory use was not found.")
        if revision not in use["exposedRevisionIds"]:
            raise MemoryIntegrationError(
                "MEMORY_REVISION_NOT_EXPOSED", 422, "The revision was not exposed to this request."
            )
        if type(weight) not in {int, float} or not math.isfinite(weight) or not 0 < weight <= 10:
            raise MemoryIntegrationError("INVALID_MEMORY_INPUT", 400, "Invalid outcome weight.")
        event = event_id(event)
        await self.enabled_control(actor)
        async with self.connection() as port:
            await self.negotiate(port, ["experience.outcome"], event)
            try:
                begun = await self.repository.begin_feedback(
                    {
                        "projectId": project,
                        "requestId": request,
                        "subjectId": actor,
                        "revisionId": revision,
                        "eventId": event,
                        "kind": kind,
                    }
                )
            except ValueError as cause:
                raise MemoryIntegrationError("MEMORY_FEEDBACK_CONFLICT", 409, str(cause)) from cause
            receipt = begun["receipt"]
            if not begun["shouldSubmit"] and receipt["outcome_id"]:
                return {"outcomeId": str(receipt["outcome_id"]), "idempotentReplay": True}
            try:
                outcome = await port.record_outcome(
                    {
                        "tenantId": use["tenantId"],
                        "subjectId": actor,
                        "traceId": use["traceId"],
                        "revisionId": revision,
                        **({"usageId": use["providerUsageId"]} if use["providerUsageId"] else {}),
                        "kind": kind,
                        "idempotencyKey": f"alphadeck:{event}:outcome",
                        "weight": weight,
                        "purpose": self.config["purpose"],
                        **({"note": js_slice(note.strip(), 0, 4096)} if note is not None else {}),
                        **({"occurredAt": occurred_at} if occurred_at else {}),
                    },
                    event,
                )
                await self.repository.complete_feedback(receipt["id"], outcome["outcomeId"])
                return outcome
            except Exception as cause:
                with suppress(Exception):
                    await self.repository.fail_feedback(
                        receipt["id"], getattr(cause, "code", "INTEGRATION_ERROR")
                    )
                raise
