import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from alphadeck_platform.memory_http import MemoryHttp, MemoryHttpError, MemoryTokens
from alphadeck_platform.memory_policy import (
    detect_candidate,
    preference_context,
    select_projection,
)

CASES = json.loads((Path(__file__).parent / "fixtures/memory.json").read_text())


@pytest.mark.parametrize(
    "case", CASES["http"], ids=lambda case: case["method"] + ":" + case["variant"]
)
def test_external_memory_http_contract_matches_original(case):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            case["status"], content=case["raw"].encode(), headers={"x-request-id": "memory-fixture"}
        )

    async def run():
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client,
            MemoryHttp(CASES["config"], client) as adapter,
        ):
            method = getattr(adapter, re.sub(r"(?<!^)(?=[A-Z])", "_", case["method"]).lower())
            try:
                actual = {
                    "result": await method("request 中文/1")
                    if case["method"] in {"discover", "checkReady"}
                    else await method(case["input"], "request 中文/1")
                }
            except MemoryHttpError as error:
                actual = {
                    "error": {
                        "message": str(error),
                        "code": error.code,
                        "status": error.status,
                        "requestId": error.request_id,
                    }
                }
        assert actual == case["expected"]
        assert len(calls) == 1
        expected = case["calls"][0]
        assert str(calls[0].url) == expected["url"]
        assert calls[0].method == expected["method"]
        for key, value in expected["headers"].items():
            assert calls[0].headers[key] == value
        if "authorization" not in expected["headers"]:
            assert "authorization" not in calls[0].headers
        if "body" in expected:
            assert json.loads(calls[0].content) == expected["body"]

    asyncio.run(run())


@pytest.mark.parametrize("case", CASES["candidates"])
def test_memory_candidate_detection_matches_original(case):
    assert detect_candidate(case["instruction"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["contexts"])
def test_memory_context_owns_project_identity(case):
    input = case["input"]
    assert (
        preference_context(input["projectId"], input["scope"], input["context"]) == case["expected"]
    )


@pytest.mark.parametrize("case", CASES["projections"])
def test_external_preference_values_cannot_cross_project_boundary(case):
    assert select_projection(case["projection"], case["projectId"]) == case["expected"]


def test_scoped_token_cache_never_shares_credentials_between_subjects():
    config = {
        **CASES["config"],
        "tokenBroker": {
            "url": "https://broker.example/token",
            "clientId": "fixture",
            "clientSecret": "synthetic-fixture-only",
            "audience": "memory",
        },
    }
    calls, now = [], [1000]

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["requested_role"] == "subject_self"
        return httpx.Response(
            200,
            json={
                "access_token": "fixture-" + body["subject_id"],
                "token_type": "Bearer",
                "expires_in": 60,
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = MemoryTokens(config, client, clock=lambda: now[0])
            scope = {"tenantId": "tenant-a", "subjectId": "actor-a", "purpose": "personalization"}
            assert await provider.token_for(scope) == "fixture-actor-a"
            assert await provider.token_for(scope) == "fixture-actor-a"
            assert len(calls) == 1
            assert await provider.token_for({**scope, "subjectId": "actor-b"}) == "fixture-actor-b"
            now[0] += 31
            assert await provider.token_for(scope) == "fixture-actor-a"
            assert len(calls) == 3

    asyncio.run(run())
