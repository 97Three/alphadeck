import asyncio
import json
from pathlib import Path

import pytest

from alphadeck_platform import context_use
from alphadeck_platform.knowledge_http import KnowledgeIntegrationError
from alphadeck_platform.knowledge_service import KnowledgeService
from alphadeck_platform.prefetch_transport import WorkspaceArtifacts

FIXTURES = Path(__file__).parent / "fixtures"
CASES = json.loads((FIXTURES / "context-services.json").read_text())
HTTP = json.loads((FIXTURES / "knowledge-http.json").read_text())


@pytest.mark.parametrize("case", CASES["preparations"], ids=lambda case: case["variant"])
def test_knowledge_preparation_and_scope_gates_match_original(case):
    calls = []

    class Port:
        async def discover(self, request=None):
            if case["variant"] == "required-failure":
                raise OSError("unavailable")
            return {
                **HTTP["discovery"],
                "supportedExtensions": [
                    value["uri"] for value in HTTP["discovery"]["supportedExtensions"]
                ],
                "operations": []
                if case["variant"] == "incompatible"
                else HTTP["discovery"]["operations"],
            }

        async def create_context_pack(self, input, request):
            calls.append({"input": input, "request": request})
            return case["pack"]

    async def run():
        service = KnowledgeService(config=case["config"], port=Port())
        try:
            expected = {
                "result": await service.prepare("  Analyse 600519  ", "request-1", case["scope"])
            }
        except KnowledgeIntegrationError as error:
            expected = {
                "error": {"code": error.code, "status": error.status, "message": str(error)}
            }
        assert expected == case["expected"]
        assert calls == case["calls"]

    asyncio.run(run())


@pytest.mark.parametrize("case", CASES["usages"], ids=lambda case: case["variant"])
def test_knowledge_usage_receipts_match_original(case):
    calls = []

    class Port:
        async def discover(self, request=None):
            return {
                **HTTP["discovery"],
                "supportedExtensions": [
                    value["uri"] for value in HTTP["discovery"]["supportedExtensions"]
                ],
            }

        async def record_usage(self, input, key, request):
            calls.append({"input": input, "key": key, "request": request})
            return {
                **HTTP["usage"],
                "exposureReceiptId": "other"
                if case["variant"] == "mismatch"
                else input["exposureReceiptId"],
                "spaceId": input["spaceId"],
            }

    async def run():
        service = KnowledgeService(config=case["config"], port=Port())
        assert (
            await service.record_usage(
                case["capsule"],
                "request-1",
                "quant-research",
                occurred_at="2026-11-03T00:00:00.000Z",
            )
            == case["expected"]
        )
        assert calls == case["calls"]

    asyncio.run(run())


def test_context_exposure_and_acceptance_transitions_match_original(tmp_path, monkeypatch):
    monkeypatch.setattr(context_use, "timestamp", lambda: "2026-11-03T00:00:00.000Z")

    async def authority(operation):
        return await operation(None)

    async def run():
        artifacts = WorkspaceArtifacts(tmp_path, authority)
        for frame in CASES["frames"]:
            input = frame["input"]
            try:
                result = (
                    await context_use.record_exposure(
                        artifacts,
                        input["projectId"],
                        input["requestId"],
                        input["integrationScope"],
                        input["memory"],
                        input["knowledge"],
                    )
                    if frame["kind"] == "expose"
                    else await context_use.record_acceptance(
                        artifacts,
                        input["projectId"],
                        input["requestId"],
                        input["mission"],
                        input["knowledgeUsage"],
                        scope=input["integrationScope"],
                    )
                )
                expected = {"result": result}
            except ValueError as error:
                expected = {"error": str(error)}
            assert expected == frame["expected"]
            document = artifacts.read("evidence/context-uses/request-1.json")
            assert document == frame["document"]

    asyncio.run(run())
