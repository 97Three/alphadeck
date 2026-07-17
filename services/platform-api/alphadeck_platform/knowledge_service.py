import hashlib
import logging
from contextlib import asynccontextmanager

from .integration_config import knowledge_config
from .knowledge_http import (
    KnowledgeHttp,
    KnowledgeHttpError,
    KnowledgeIntegrationError,
    compatibility_issues,
)
from .messages import js_slice
from .prefetch_market import timestamp
from .provenance import js_json

PROVIDER = "akep-http-v0.1"
EVALUATOR_URI = "urn:alphadeck:evaluator:human-business-outcome:v1"
EVALUATOR_DIGEST = "sha256:" + hashlib.sha256(b"alphadeck-human-business-outcome-v1").hexdigest()


def suffix(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def failure_code(cause):
    return (
        cause.code
        if isinstance(cause, (KnowledgeHttpError, KnowledgeIntegrationError))
        else "INTEGRATION_ERROR"
    )


def task_category(value):
    return js_slice(value, 0, 255) or "quant-research"


def capsule_content(pack):
    return js_json(
        {
            "schemaVersion": 1,
            "provider": PROVIDER,
            **{
                key: pack[key]
                for key in (
                    "contextPackId",
                    "contextDigest",
                    "policyEpoch",
                    "purpose",
                    "quality",
                    "warnings",
                )
            },
            "passages": [
                {
                    key: item[key]
                    for key in (
                        "citationId",
                        "title",
                        "text",
                        "rank",
                        "score",
                        "revisionId",
                        "spaceId",
                    )
                }
                for item in pack["passages"]
            ],
            "citations": pack["citations"],
            "obligations": pack["obligations"],
        }
    )


class KnowledgeService:
    def __init__(self, *, config=None, port=None):
        self.config = config if config is not None else knowledge_config()
        self.port = port

    @asynccontextmanager
    async def connection(self):
        if self.port:
            yield self.port
        else:
            async with KnowledgeHttp(self.config) as port:
                yield port

    async def negotiate(self, port, request=None):
        info = await port.discover(request)
        issues = compatibility_issues(info, self.config)
        if issues:
            raise KnowledgeIntegrationError(
                "KNOWLEDGE_INCOMPATIBLE",
                503,
                "Governed knowledge service is incompatible: " + ", ".join(issues) + ".",
            )
        return info

    async def inspect(self, request=None):
        if not self.config["enabled"]:
            return
        async with self.connection() as port:
            await self.negotiate(port, request)
            await port.check_ready(request)

    async def prepare(self, task, request, scope):
        if not self.config["enabled"]:
            return {"status": "disabled", "capsule": None, "passageCount": 0, "citationCount": 0}
        try:
            async with self.connection() as port:
                await self.negotiate(port, request)
                if not task.strip():
                    raise KnowledgeIntegrationError(
                        "INVALID_KNOWLEDGE_TASK", 400, "Knowledge task is empty."
                    )
                pack = await port.create_context_pack(
                    {
                        "task": js_slice(task.strip(), 0, 4000),
                        "purpose": self.config["purpose"],
                        "spaces": scope["knowledge"]["requestedSpaceIds"],
                        "maxCharacters": self.config["maxContextCharacters"],
                        "supportedObligations": list(self.config["supportedObligations"]),
                    },
                    request,
                )
            if pack["purpose"] != self.config["purpose"]:
                raise KnowledgeIntegrationError(
                    "KNOWLEDGE_PURPOSE_MISMATCH", 502, "AKEP returned a mismatched purpose."
                )
            if any(
                item["spaceId"] not in scope["knowledge"]["requestedSpaceIds"]
                for item in [*pack["passages"], *pack["citations"]]
            ):
                raise KnowledgeIntegrationError(
                    "KNOWLEDGE_SCOPE_MISMATCH",
                    502,
                    "AKEP returned content outside the requested project knowledge scope.",
                )
            if not pack["passages"] or not pack["citations"]:
                return {
                    "status": "empty",
                    "capsule": None,
                    "passageCount": len(pack["passages"]),
                    "citationCount": len(pack["citations"]),
                }
            capsule = {
                "content": capsule_content(pack),
                **{
                    key: pack[key]
                    for key in (
                        "contextPackId",
                        "contextDigest",
                        "exposureReceiptId",
                        "policyEpoch",
                        "purpose",
                        "citations",
                        "obligations",
                    )
                },
                "qualityDecision": pack["quality"]["decision"],
                "warningCodes": [warning["code"] for warning in pack["warnings"]],
                "integrationScopeSha256": scope["scopeSha256"],
                "consumerId": scope["consumerId"],
                "requestedSpaceIds": scope["knowledge"]["requestedSpaceIds"],
                "projectSpaceId": scope["knowledge"]["projectSpaceId"],
            }
            return {
                "status": "prepared",
                "capsule": capsule,
                "passageCount": len(pack["passages"]),
                "citationCount": len(pack["citations"]),
            }
        except Exception as cause:
            if self.config["required"]:
                raise KnowledgeIntegrationError(
                    "KNOWLEDGE_REQUIRED_UNAVAILABLE",
                    503,
                    "Required governed knowledge is unavailable.",
                ) from cause
            code = failure_code(cause)
            logging.warning("Optional governed knowledge unavailable (%s)", code)
            return {
                "status": "unavailable",
                "capsule": None,
                "passageCount": 0,
                "citationCount": 0,
                "failureCode": code,
            }

    async def record_usage(self, capsule, request, category, *, occurred_at=None):
        if not self.config["enabled"]:
            return {"status": "disabled", "usageReceipts": []}
        if not capsule or not capsule["citations"]:
            return {"status": "not_applicable", "usageReceipts": []}
        try:
            async with self.connection() as port:
                await self.negotiate(port, request)
                by_space = {}
                for citation in capsule["citations"]:
                    by_space.setdefault(citation["spaceId"], []).append(citation)
                occurred_at = occurred_at or timestamp()
                receipts = []
                for space, citations in by_space.items():
                    stable = suffix(space)
                    receipt = await port.record_usage(
                        {
                            "clientUsageId": f"alphadeck:{request}:{stable}",
                            "exposureReceiptId": capsule["exposureReceiptId"],
                            "spaceId": space,
                            "citations": [
                                {
                                    **{
                                        key: citation[key]
                                        for key in (
                                            "citationId",
                                            "revisionId",
                                            "payloadDigest",
                                            "locator",
                                        )
                                    },
                                    "influence": "seen",
                                }
                                for citation in citations
                            ],
                            "taskCategory": task_category(category),
                            "purpose": capsule["purpose"],
                            "contextDigest": capsule["contextDigest"],
                            "occurredAt": occurred_at,
                        },
                        f"alphadeck-knowledge-usage-{request}-{stable}",
                        request,
                    )
                    if (
                        receipt["exposureReceiptId"] != capsule["exposureReceiptId"]
                        or receipt["spaceId"] != space
                        or receipt["policyEpoch"] != capsule["policyEpoch"]
                    ):
                        raise KnowledgeIntegrationError(
                            "KNOWLEDGE_USAGE_MISMATCH",
                            502,
                            "Knowledge usage receipt does not match the delivered context.",
                        )
                    receipts.append(receipt)
            return {"status": "recorded", "usageReceipts": receipts}
        except Exception as cause:
            code = failure_code(cause)
            logging.warning("Knowledge usage evidence unavailable (%s)", code)
            return {"status": "unavailable", "usageReceipts": [], "failureCode": code}

    async def record_feedback(self, input):
        outcome = input["outcome"]
        if (
            input["usage"]["status"] != "recorded"
            or not input["citations"]
            or not self.config["enabled"]
        ):
            return {"status": "not_applicable", "outcome": outcome, "feedbackReceipts": []}
        request = input["requestId"]
        try:
            async with self.connection() as port:
                info = await self.negotiate(port, request)
                if "feedback" not in info["operations"]:
                    raise KnowledgeIntegrationError(
                        "KNOWLEDGE_FEEDBACK_INCOMPATIBLE",
                        503,
                        "Governed knowledge service does not advertise feedback.",
                    )
                receipts = []
                for usage in input["usage"]["usageReceipts"]:
                    citations = [
                        item for item in input["citations"] if item["spaceId"] == usage["spaceId"]
                    ]
                    if not citations:
                        raise KnowledgeIntegrationError(
                            "KNOWLEDGE_FEEDBACK_MISMATCH",
                            502,
                            "Knowledge usage receipt has no matching delivered citations.",
                        )
                    stable = suffix(request + ":" + usage["usageId"])
                    feedback = "urn:alphadeck:knowledge-feedback:" + stable
                    receipt = await port.record_feedback(
                        {
                            "feedbackId": feedback,
                            "usageId": usage["usageId"],
                            "citations": [
                                {
                                    key: item[key]
                                    for key in (
                                        "citationId",
                                        "revisionId",
                                        "payloadDigest",
                                        "locator",
                                    )
                                }
                                for item in citations
                            ],
                            "taskCategory": task_category(input["taskCategory"]),
                            "outcome": outcome,
                            "metrics": [
                                {
                                    "name": "business.user_outcome",
                                    "value": 1
                                    if outcome == "helped"
                                    else -1
                                    if outcome == "harmed"
                                    else 0,
                                    "unit": "score",
                                }
                            ],
                            "evaluatorVersion": {"uri": EVALUATOR_URI, "digest": EVALUATOR_DIGEST},
                            "contextDigest": input["contextDigest"],
                            "evidenceRefs": [
                                "urn:alphadeck:mission-receipt:"
                                + suffix(
                                    input["acceptedReceiptId"]
                                    + ":"
                                    + input["acceptedReceiptSha256"]
                                ),
                                "urn:alphadeck:business-feedback:" + suffix(input["eventId"]),
                            ],
                            "observedAt": input.get("observedAt") or timestamp(),
                            "privacy": {"rawTaskStored": False, "aggregation": "pseudonymized"},
                        },
                        f"alphadeck-knowledge-feedback-{request}-{stable}",
                        request,
                    )
                    if (
                        receipt["feedbackId"] != feedback
                        or receipt["usageId"] != usage["usageId"]
                        or receipt["evaluatorVersion"]
                        != {"uri": EVALUATOR_URI, "digest": EVALUATOR_DIGEST}
                    ):
                        raise KnowledgeIntegrationError(
                            "KNOWLEDGE_FEEDBACK_MISMATCH",
                            502,
                            "Knowledge feedback receipt does not match the submitted Mission evidence.",
                        )
                    receipts.append(receipt)
            return {"status": "recorded", "outcome": outcome, "feedbackReceipts": receipts}
        except Exception as cause:
            code = failure_code(cause)
            logging.warning("Knowledge business outcome unavailable (%s)", code)
            return {
                "status": "unavailable",
                "outcome": outcome,
                "feedbackReceipts": [],
                "failureCode": code,
            }


async def write_knowledge_evidence(artifacts, request, preparation, usage=None):
    capsule = preparation["capsule"]
    document = {
        "schemaVersion": 1,
        "provider": PROVIDER,
        "requestId": request,
        **{key: preparation[key] for key in ("status", "passageCount", "citationCount")},
        **({"failureCode": preparation["failureCode"]} if preparation.get("failureCode") else {}),
        **(
            {
                key: capsule[key]
                for key in (
                    "contextPackId",
                    "contextDigest",
                    "exposureReceiptId",
                    "policyEpoch",
                    "purpose",
                    "qualityDecision",
                    "obligations",
                    "warningCodes",
                    "citations",
                )
            }
            if capsule
            else {"citations": []}
        ),
        **({"usage": usage} if usage is not None else {}),
        "recordedAt": timestamp(),
    }
    await artifacts.write("evidence/knowledge-sources.json", document)
