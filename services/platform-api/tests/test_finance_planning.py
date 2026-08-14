import asyncio
import copy
import json
from pathlib import Path

import pytest

from alphadeck_platform.finance_intent import assess_intent, clarification_message
from alphadeck_platform.finance_planning import write_initial_plan

CASES = json.loads((Path(__file__).parent / "fixtures/finance-planning.json").read_text())


class Artifacts:
    def __init__(self):
        self.entries, self.events = {}, []

    def read(self, relative):
        return copy.deepcopy(self.entries.get(relative))

    async def write(self, relative, value):
        self.entries[relative] = copy.deepcopy(value)
        return relative

    async def event(self, value):
        self.events.append(copy.deepcopy(value))


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_research_plan_and_neutral_projections_match_original(case, monkeypatch):
    monkeypatch.setattr(
        "alphadeck_platform.finance_planning.timestamp", lambda: "2026-11-03T00:00:00.000Z"
    )

    async def run():
        artifacts = Artifacts()
        try:
            plan = await write_initial_plan(copy.deepcopy(case["params"]), artifacts)
            actual = {"plan": plan, "entries": artifacts.entries, "events": artifacts.events}
        except ValueError as cause:
            actual = {"error": str(cause)}
        assert actual == case["expected"]

    asyncio.run(run())


@pytest.mark.parametrize("case", CASES["intent"])
def test_financial_intent_and_clarification_match_original(case):
    value = assess_intent(case["input"])
    assert value == case["expected"]
    assert clarification_message(value) == case["message"]
