import copy
import json
from pathlib import Path

import pytest

from alphadeck_platform.baseline_evidence import FINAL, QUALITY, SOURCES, ensure_baseline

pytestmark = pytest.mark.anyio

CASES = json.loads((Path(__file__).parent / "fixtures/baseline-evidence.json").read_text())


class Artifacts:
    def __init__(self, entries=None):
        self.entries = copy.deepcopy(entries or {})
        self.events = []

    def read(self, path):
        return copy.deepcopy(self.entries.get(path))

    async def write(self, path, value):
        self.entries[path] = copy.deepcopy(value)
        return path

    async def event(self, value):
        self.events.append(value)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
async def test_baseline_evidence_matches_original(case):
    artifacts = Artifacts({FINAL: case["input"], ".data-agent/finance-run-plan.json": case["plan"]})
    result = await ensure_baseline(artifacts, force=True, now="2025-01-01T00:00:00.000Z")
    assert {
        "outcome": result,
        "sources": artifacts.entries[SOURCES],
        "quality": artifacts.entries[QUALITY],
    } == case["expected"]
    assert artifacts.events[-1]["event_type"] == "data_quality_checked"
    assert (await ensure_baseline(artifacts))["created"] is False


async def test_missing_final_does_not_claim_evidence_created():
    assert (await ensure_baseline(Artifacts()))["created"] is False
