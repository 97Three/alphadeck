import asyncio
import json
from pathlib import Path

import httpx
import pytest

from alphadeck_platform.provenance import js_json
from alphadeck_platform.quant_output import compact_quant_output
from alphadeck_platform.quant_tool import QuantApiTool, parse_quant_input
from alphadeck_platform.tool_paths import ToolError

FIXTURES = Path(__file__).parent / "fixtures"
OUTPUT = json.loads((FIXTURES / "quant-output.json").read_text())
TOOLS = json.loads((FIXTURES / "quant-tool.json").read_text())


def test_json_stringify_joins_surrogate_halves_created_by_window_slices():
    assert js_json("\ud83d\ude00") == '"😀"'
    assert js_json("\ud83d") == '"\\ud83d"'


@pytest.mark.parametrize("case", OUTPUT)
def test_quant_output_windows_match_original(case):
    options = case["options"]
    assert (
        compact_quant_output(
            case["value"],
            limit=options["limit"],
            response_bytes=options["responseBytes"],
            response_byte_limit_reached=options["responseByteLimitReached"],
            path=options["path"],
        )
        == case["expected"]
    )


@pytest.mark.parametrize("case", TOOLS, ids=lambda case: case["name"])
def test_quant_tool_execution_receipts_and_boundaries_match_original(case, monkeypatch):
    monkeypatch.setattr(
        "alphadeck_platform.quant_tool.timestamp", lambda: "2026-11-03T00:00:00.000Z"
    )
    monkeypatch.setenv("ALPHADECK_MARKET_API_URL", "http://127.0.0.1:8100")

    async def run():
        calls = []

        async def upstream(request):
            calls.append(str(request.url).replace(":8100/", ":8000/"))
            return httpx.Response(case.get("status", 200), content=case["body"].encode())

        options = {"maxResponseBytes": "max_response_bytes", "maxRequests": "max_requests"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            tool = QuantApiTool(
                client=client,
                **{options[key]: value for key, value in case.get("options", {}).items()},
            )
            actual = []
            for _ in range(case.get("repeat", 1)):
                try:
                    value = parse_quant_input(case["input"])
                    actual.append(await tool.execute(value))
                except ToolError as error:
                    actual.append({"parseError": {"code": error.code, "message": error.message}})
        assert actual == case["expected"]
        assert calls == case["calls"]

    asyncio.run(run())
