import asyncio
import copy
import json
from uuid import uuid4

import httpx
import pytest
from test_run_engine import engine_context  # noqa: F401

from alphadeck_platform.auth import Authorization
from alphadeck_platform.integration_config import (
    create_integration_scope,
    knowledge_config,
    memory_config,
)
from alphadeck_platform.memory_http import MemoryHttpError
from alphadeck_platform.memory_repository import MemoryRepository
from alphadeck_platform.memory_service import CAPABILITIES, MemoryIntegrationError, MemoryService
from app import create_app


class MemoryPort:
    def __init__(self, project):
        self.project = project
        self.trace, self.policy, self.revision, self.usage, self.outcome = [
            str(uuid4()) for _ in range(5)
        ]
        self.calls, self.fail_outcome, self.forged_usage = [], False, False

    async def discover(self, request=None):
        self.calls.append("discover")
        return {
            "name": "Memory",
            "version": "fixture",
            "apiContract": "evolvable-memory-http/v1",
            "capabilities": CAPABILITIES,
            "authMode": "development",
            "scopeSource": "request",
            "productionReady": False,
            "productionBlockers": [],
        }

    async def check_ready(self, request=None):
        return None

    async def recall(self, input, request=None):
        self.calls.append(("recall", input))
        return {
            "traceId": self.trace,
            "policyId": self.policy,
            "policyVersion": 1,
            "validAt": "2026-11-03T00:00:00Z",
            "knownAt": "2026-11-03T00:00:00Z",
            "items": [],
        }

    async def project_context(self, input, request=None):
        self.calls.append(("project_context", input))
        return {
            "traceId": self.trace,
            "projectionSha256": "a" * 64,
            "segments": [
                {
                    "content": json.dumps(
                        {
                            "key": "output.detail_level",
                            "value": "concise",
                            "context": {"product": "alphadeck", "project_id": self.project},
                        }
                    ),
                    "sources": [{"revisionId": self.revision}],
                }
            ],
        }

    async def record_usage(self, input, request=None):
        self.calls.append(("record_usage", input))
        return {
            "usageId": self.usage,
            "traceId": input["traceId"],
            "algorithm": input["algorithm"],
            "maxCharacters": input["maxCharacters"],
            "sourceProjectionSha256": input["sourceProjectionSha256"],
            "deliveredContextSha256": "forged"
            if self.forged_usage
            else input["deliveredContextSha256"],
            "revisionIds": input["revisionIds"],
        }

    async def list_preferences(self, input, request=None):
        return [
            {
                "recordId": self.revision,
                "key": "output.detail_level",
                "value": "concise",
                "context": {"product": "alphadeck"},
            }
        ]

    async def remember_preference(self, input, request=None):
        self.calls.append(("remember", input))
        return {
            "observationId": "observation",
            "candidateId": "candidate",
            "recordId": self.revision,
            "revisionId": self.revision,
            "sequence": 1,
            "idempotentReplay": False,
        }

    async def correct_preference(self, input, request=None):
        self.calls.append(("correct", input))
        return {
            "recordId": self.revision,
            "revisionId": self.revision,
            "sequence": 2,
            "idempotentReplay": False,
        }

    async def get_revisions(self, input, request=None):
        return [{"id": self.revision, "value": "concise", "sequence": 1}]

    async def record_outcome(self, input, request=None):
        self.calls.append(("record_outcome", input))
        if self.fail_outcome:
            self.fail_outcome = False
            raise MemoryHttpError("Unavailable", None, "NETWORK_ERROR")
        return {"outcomeId": self.outcome, "idempotentReplay": False}


def runtime(database, project, *, required=False):
    config = {**memory_config({}), "required": required}
    port = MemoryPort(project)

    def scope(identity):
        return create_integration_scope(identity, config, knowledge_config({}), {})

    return MemoryService(database, config=config, port=port, scope_factory=scope), port


def test_recall_is_not_exposure_and_opt_out_blocks_new_use(engine_context):  # noqa: F811
    database, input, _, _ = engine_context
    service, port = runtime(database, input["projectId"])
    repository, actor = MemoryRepository(database), str(uuid4())

    async def run():
        try:
            recall = await service.recall(input["projectId"], actor, input["requestId"], "research")
            assert recall["status"] == "opted_out" and not port.calls
            await repository.set_control(actor, True)
            recall = await service.recall(input["projectId"], actor, input["requestId"], "research")
            assert recall["status"] == "prepared"
            assert await repository.find_use(input["projectId"], input["requestId"]) is None
            await repository.set_control(actor, False)
            assert (
                await service.expose(input["projectId"], actor, input["requestId"], recall) is None
            )
            assert not any(
                isinstance(call, tuple) and call[0] == "record_usage" for call in port.calls
            )
            await repository.set_control(actor, True)
            capsule = await service.expose(input["projectId"], actor, input["requestId"], recall)
            assert capsule["usageId"] == port.usage
            use = await repository.find_use(input["projectId"], input["requestId"])
            assert use["providerUsageId"] == port.usage
            assert (await repository.summary(actor))["exposedRunCount"] == 1
            assert (
                await service.expose(input["projectId"], actor, input["requestId"], recall)
                == capsule
            )
            forged = copy.deepcopy(recall)
            forged["preparedUse"]["subjectId"] = "other-actor"
            with pytest.raises(MemoryIntegrationError) as error:
                await service.expose(input["projectId"], actor, input["requestId"], forged)
            assert error.value.code == "MEMORY_USE_MISMATCH"
            with pytest.raises(MemoryIntegrationError):
                await service.use_attribution(input["projectId"], "other-actor", input["requestId"])
        finally:
            async with database.transaction() as tx:
                await tx.execute(
                    "DELETE FROM personal_memory_controls WHERE subject_id=%s", (actor,)
                )

    asyncio.run(run())


@pytest.mark.parametrize("required", [False, True])
def test_mismatched_provider_usage_never_exposes_memory(engine_context, required):  # noqa: F811
    database, input, _, _ = engine_context
    service, port = runtime(database, input["projectId"], required=required)
    actor = str(uuid4())

    async def run():
        try:
            await service.repository.set_control(actor, True)
            recall = await service.recall(input["projectId"], actor, input["requestId"], "research")
            port.forged_usage = True
            if required:
                with pytest.raises(MemoryIntegrationError) as error:
                    await service.expose(input["projectId"], actor, input["requestId"], recall)
                assert error.value.code == "MEMORY_REQUIRED_UNAVAILABLE"
            else:
                assert (
                    await service.expose(input["projectId"], actor, input["requestId"], recall)
                    is None
                )
            assert await service.repository.find_use(input["projectId"], input["requestId"]) is None
        finally:
            async with database.transaction() as tx:
                await tx.execute(
                    "DELETE FROM personal_memory_controls WHERE subject_id=%s", (actor,)
                )

    asyncio.run(run())


def test_feedback_retries_the_same_provider_key_and_rejects_conflicts(engine_context):  # noqa: F811
    database, input, _, _ = engine_context
    service, port = runtime(database, input["projectId"])
    actor = str(uuid4())

    async def run():
        try:
            await service.repository.set_control(actor, True)
            recall = await service.recall(input["projectId"], actor, input["requestId"], "research")
            await service.expose(input["projectId"], actor, input["requestId"], recall)
            port.fail_outcome = True
            arguments = (
                input["projectId"],
                actor,
                input["requestId"],
                port.revision,
                "feedback-1",
                "helpful",
            )
            with pytest.raises(MemoryHttpError):
                await service.feedback(*arguments)
            assert (await service.repository.summary(actor))["failedFeedbackCount"] == 1
            first = await service.feedback(*arguments)
            replay = await service.feedback(*arguments)
            assert (
                first["outcomeId"] == replay["outcomeId"] == port.outcome
                and replay["idempotentReplay"]
            )
            calls = [
                call[1]
                for call in port.calls
                if isinstance(call, tuple) and call[0] == "record_outcome"
            ]
            assert len(calls) == 2 and calls[0]["idempotencyKey"] == calls[1]["idempotencyKey"]
            with pytest.raises(MemoryIntegrationError) as error:
                await service.feedback(*arguments[:-1], "rejected")
            assert error.value.code == "MEMORY_FEEDBACK_CONFLICT"
            with pytest.raises(MemoryIntegrationError) as error:
                await service.feedback(
                    input["projectId"],
                    actor,
                    input["requestId"],
                    str(uuid4()),
                    "feedback-2",
                    "helpful",
                )
            assert error.value.code == "MEMORY_REVISION_NOT_EXPOSED"
        finally:
            async with database.transaction() as tx:
                await tx.execute(
                    "DELETE FROM personal_memory_controls WHERE subject_id=%s", (actor,)
                )

    asyncio.run(run())


def test_memory_api_uses_authenticated_actor_and_project_permissions(
    engine_context,  # noqa: F811
    actor,
    monkeypatch,
):
    database, input, _, root = engine_context
    service, port = runtime(database, input["projectId"])
    monkeypatch.setenv("ALPHADECK_MEMORY_ENABLED", "1")
    app = create_app(
        database,
        Authorization(database, enabled=True, secret="test-secret"),
        root,
        memory_service_factory=lambda database, config: service,
    )

    async def run():
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost:3100",
                cookies={"alphadeck.session_token": actor["cookie"]},
            ) as client:
                control = await client.get("/api/account/memory")
                assert control.status_code == 200, control.text
                assert control.headers["cache-control"] == "private, no-store"
                assert control.json()["data"]["control"]["personalizationEnabled"] is False
                update = await client.put(
                    "/api/account/memory", json={"personalizationEnabled": True}
                )
                assert update.status_code == 200 and update.json()["data"]["changed"]
                preferences = f"/api/projects/{input['projectId']}/memory/preferences"
                assert (await client.get(preferences)).json()["data"][0][
                    "recordId"
                ] == port.revision
                remembered = await client.post(
                    preferences,
                    json={
                        "eventId": "preference-1",
                        "key": "output.detail_level",
                        "value": "concise",
                        "evidenceText": "Keep concise",
                        "scope": "project",
                        "actorUserId": "forged",
                        "context": {"product": "forged", "project_id": "other"},
                    },
                )
                assert remembered.status_code == 201, remembered.text
                call = next(
                    call[1]
                    for call in port.calls
                    if isinstance(call, tuple) and call[0] == "remember"
                )
                assert call["subjectId"] == actor["user_id"]
                assert call["context"] == {
                    "product": "alphadeck",
                    "project_id": input["projectId"],
                }
                assert (
                    await client.get(preferences + f"/{port.revision}/revisions")
                ).status_code == 200
                corrected = await client.post(
                    f"/api/account/memory/preferences/{port.revision}/corrections",
                    json={
                        "eventId": "correction-1",
                        "value": "detailed",
                        "evidenceText": "Be detailed",
                        "reason": "changed preference",
                    },
                )
                assert corrected.status_code == 201, corrected.text
                assert (await client.post(preferences, json={"scope": {}})).status_code == 400
                assert (
                    await client.post(
                        f"/api/projects/{input['projectId']}/memory/outcomes", json={"kind": {}}
                    )
                ).status_code == 400
                assert (
                    await client.get("/api/projects/not-owned/memory/preferences")
                ).status_code == 404
        finally:
            async with database.transaction() as tx:
                await tx.execute(
                    "DELETE FROM personal_memory_controls WHERE subject_id=%s", (actor["user_id"],)
                )

    asyncio.run(run())
