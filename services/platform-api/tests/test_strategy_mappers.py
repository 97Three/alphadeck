import json
from pathlib import Path

import pytest

from alphadeck_platform import strategy_mappers

CASES = json.loads((Path(__file__).parent / "fixtures/strategy-mappers.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_market_to_strategy_projection_matches_original(case, monkeypatch):
    monkeypatch.setattr(strategy_mappers, "now", lambda: "2026-11-03T00:00:00.000Z")
    assert strategy_mappers.mapped(case["name"], *case["args"]) == case["expected"]
