import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from alphadeck_platform.knowledge_http import KnowledgeHttp, KnowledgeHttpError

CASES = json.loads((Path(__file__).parent / "fixtures/knowledge-http.json").read_text())


@pytest.mark.parametrize(
    "case", CASES["cases"], ids=lambda value: value["method"] + ":" + value["variant"]
)
def test_akep_http_contract_matches_original(case):
    requests = []

    def respond(request):
        requests.append(request)
        if case["method"] != "discover" and request.url.path == "/.well-known/akep":
            return httpx.Response(
                200, json=CASES["discovery"], headers={"x-request-id": "knowledge-fixture"}
            )
        return httpx.Response(
            case["status"],
            content=case["raw"].encode(),
            headers={"x-request-id": "knowledge-fixture"},
        )

    async def run():
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client,
            KnowledgeHttp(CASES["config"], client) as adapter,
        ):
            function = getattr(adapter, re.sub(r"(?<!^)(?=[A-Z])", "_", case["method"]).lower())
            try:
                result = (
                    await function("request 中文/1")
                    if case["method"] in {"discover", "checkReady"}
                    else await function(case["input"], "request 中文/1")
                    if case["method"] == "createContextPack"
                    else await function(case["input"], "idempotency-1", "request 中文/1")
                )
                actual = {"result": result}
            except KnowledgeHttpError as error:
                actual = {
                    "error": {
                        "message": str(error),
                        "code": error.code,
                        "status": error.status,
                        "requestId": error.request_id,
                        "traceId": error.trace_id,
                    }
                }
        assert actual == case["expected"]
        assert len(requests) == len(case["calls"])
        for request, expected in zip(requests, case["calls"], strict=True):
            assert str(request.url) == expected["url"]
            assert request.method == expected["method"]
            for key, value in expected["headers"].items():
                assert request.headers[key] == value
            if "authorization" not in expected["headers"]:
                assert "authorization" not in request.headers
            if "body" in expected:
                assert json.loads(request.content) == expected["body"]

    asyncio.run(run())


@pytest.mark.parametrize("case", CASES["endpointCases"])
def test_discovery_cannot_redirect_credentials_to_an_untrusted_service(case):
    async def run():
        def response(_):
            return httpx.Response(200, json={**CASES["discovery"], "baseUrl": case["baseUrl"]})

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(response)) as client,
            KnowledgeHttp(CASES["config"], client) as adapter,
        ):
            try:
                actual = {"result": await adapter.discover("request-1")}
            except KnowledgeHttpError as error:
                actual = {"code": error.code}
            assert actual == case["expected"]

    asyncio.run(run())
