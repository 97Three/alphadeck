import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest
from test_run_engine import engine_context  # noqa: F401

from alphadeck_platform.auth import Authorization
from alphadeck_platform.integration_config import create_integration_scope, memory_config
from alphadeck_platform.knowledge_growth import KnowledgeGrowth
from alphadeck_platform.knowledge_http import KnowledgeHttpError
from alphadeck_platform.knowledge_repository import KnowledgeGrowthError
from alphadeck_platform.knowledge_service import KnowledgeService
from app import create_app

HTTP = json.loads((Path(__file__).parent / "fixtures/knowledge-http.json").read_text())
CONTEXT = json.loads((Path(__file__).parent / "fixtures/context-services.json").read_text())


def runtime(database, project):
    config = HTTP["config"]
    config = {**config, "spaces": [HTTP["pack"]["citations"][0]["spaceId"]]}

    def scope(identity):
        return create_integration_scope(identity, memory_config({}), config, {})

    class Port:
        def __init__(self):
            self.calls, self.fail_once = [], False

        async def discover(self, request=None):
            return {
                **HTTP["discovery"],
                "supportedExtensions": [
                    item["uri"] for item in HTTP["discovery"]["supportedExtensions"]
                ],
            }

        async def record_feedback(self, input, key, request):
            self.calls.append((input, key, request))
            if self.fail_once:
                self.fail_once = False
                raise KnowledgeHttpError("Unavailable", None, "NETWORK_ERROR")
            return {
                **HTTP["feedback"],
                "feedbackId": input["feedbackId"],
                "usageId": input["usageId"],
                "evaluatorVersion": input["evaluatorVersion"],
            }

    port = Port()
    growth = KnowledgeGrowth(
        database, service=KnowledgeService(config=config, port=port), scope_factory=scope
    )
    capsule = copy.deepcopy(CONTEXT["preparations"][0]["expected"]["result"]["capsule"])
    scoped = scope(project)
    capsule.update(
        integrationScopeSha256=scoped["scopeSha256"],
        consumerId=scoped["consumerId"],
        requestedSpaceIds=scoped["knowledge"]["requestedSpaceIds"],
        projectSpaceId=scoped["knowledge"]["projectSpaceId"],
    )
    usage = copy.deepcopy(CONTEXT["usages"][0]["expected"])
    return growth, port, capsule, usage


def test_knowledge_attribution_is_immutable_and_feedback_is_idempotent(engine_context):  # noqa: F811
    database, input, _, _ = engine_context
    growth, port, capsule, usage = runtime(database, input["projectId"])

    async def run():
        arguments = (
            input["projectId"],
            input["requestId"],
            "quant-research",
            capsule,
            usage,
            "receipt-accepted",
            "sha256:" + "a" * 64,
        )
        first = await growth.persist_accepted(*arguments)
        assert first["citationCount"] == 1 and first["feedbackAvailable"]
        assert await growth.persist_accepted(*arguments) == first
        other_capsule = {**capsule, "contextDigest": "sha256:" + "c" * 64}
        with pytest.raises(KnowledgeGrowthError):
            await growth.persist_accepted(*arguments[:3], other_capsule, *arguments[4:])
        port.fail_once = True
        feedback = (input["projectId"], input["requestId"], "actor-1", "feedback-1", "helped")
        with pytest.raises(KnowledgeGrowthError) as unavailable:
            await growth.feedback(*feedback)
        assert unavailable.value.status == 503
        completed = await growth.feedback(*feedback)
        assert completed["feedbackStatus"] == "completed" and not completed["feedbackAvailable"]
        assert await growth.feedback(*feedback) == completed
        assert len(port.calls) == 2 and port.calls[0][1] == port.calls[1][1]
        assert (
            port.calls[0][0]["evidenceRefs"]
            and port.calls[0][0]["privacy"]["rawTaskStored"] is False
        )
        with pytest.raises(KnowledgeGrowthError) as conflict:
            await growth.feedback(*feedback[:-1], "harmed")
        assert conflict.value.code == "KNOWLEDGE_FEEDBACK_CONFLICT"

    asyncio.run(run())


def test_knowledge_api_binds_feedback_to_authorized_actor(
    engine_context,  # noqa: F811
    actor,
):
    database, input, _, root = engine_context
    growth, port, capsule, usage = runtime(database, input["projectId"])
    app = create_app(
        database,
        Authorization(database, enabled=True, secret="test-secret"),
        root,
        knowledge_growth_factory=lambda database: growth,
    )

    async def run():
        await growth.persist_accepted(
            input["projectId"],
            input["requestId"],
            "quant-research",
            capsule,
            usage,
            "receipt-accepted",
            "sha256:" + "a" * 64,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:3100",
            cookies={"alphadeck.session_token": actor["cookie"]},
        ) as client:
            path = f"/api/projects/{input['projectId']}/knowledge"
            use = await client.get(path + "/uses/" + input["requestId"])
            assert use.status_code == 200 and use.headers["cache-control"] == "private, no-store"
            assert use.json()["data"]["feedbackStatus"] == "awaiting_feedback"
            body = {
                "requestId": input["requestId"],
                "eventId": "feedback-1",
                "outcome": "helped",
                "actorUserId": "forged",
            }
            result = await client.post(path + "/outcomes", json=body)
            assert result.status_code == 201, result.text
            stored = await growth.repository.find(input["projectId"], input["requestId"])
            assert stored["feedbackActorUserId"] == actor["user_id"]
            assert (
                await client.post(path + "/outcomes", json={**body, "outcome": "harmed"})
            ).status_code == 409
            assert (await client.post(path + "/outcomes", json={"outcome": {}})).status_code == 400
            assert (
                await client.get("/api/projects/not-owned/knowledge/uses/unknown")
            ).status_code == 404

    asyncio.run(run())
