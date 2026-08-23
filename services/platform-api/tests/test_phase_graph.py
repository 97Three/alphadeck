import asyncio
import json
from pathlib import Path

import pytest

from alphadeck_platform.deterministic_provider import DeterministicProvider
from alphadeck_platform.phase_graph import classify_prepared_intent, phase_graph

CASES = json.loads((Path(__file__).parent / "fixtures/phase-graph.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_execution_lane_and_budgets_match_original(case):
    assert phase_graph(case["input"]) == case["expected"]


@pytest.mark.parametrize(
    "instruction,expected",
    [
        ("分析600519并生成看板", "standard"),
        ("分析600519风险与收益", "standard"),
        ("把看板配色改成蓝色", "custom"),
        ("不要侧栏", "custom"),
        ("Use a responsive layout", "custom"),
        ("ＣＵＳＴＯＭＩＺＥ theme", "custom"),
    ],
)
def test_prepared_presentation_changes_use_custom_lane(instruction, expected):
    assert classify_prepared_intent(instruction) == expected


@pytest.mark.parametrize("outcome", [False, None, "unrelated", "wrong-name", "bad-json"])
def test_deterministic_provider_never_advances_after_failed_or_unrelated_outcome(outcome):
    async def run():
        steps = [
            {"name": "compile", "arguments": {"nested": {"value": "original"}}},
            {"name": "submit", "arguments": {}},
        ]
        provider = DeterministicProvider(steps)
        steps[0]["arguments"]["nested"]["value"] = "mutated"
        request = {
            "model": "fixture",
            "messages": [],
            "tools": [{"name": "compile"}, {"name": "submit"}],
        }
        events = [value async for value in provider.complete(request)]
        assert json.loads(events[1]["argumentsDelta"])["nested"]["value"] == "original"
        request["messages"] = [
            {
                "role": "tool",
                "toolCallId": "unrelated" if outcome == "unrelated" else "deterministic_step_1",
                "name": "wrong" if outcome == "wrong-name" else "compile",
                "content": "invalid" if outcome == "bad-json" else json.dumps({"ok": outcome}),
            }
        ]
        events = [value async for value in provider.complete(request)]
        assert not any(value["type"] == "tool_call_delta" for value in events)
        assert events[-1] == {"type": "finish", "reason": "stop"}

    asyncio.run(run())
