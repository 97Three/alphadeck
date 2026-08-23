import json
from pathlib import Path

import pytest

from alphadeck_platform.portfolio import (
    correlation_summary,
    holding_rows,
    liquidity_summary,
    portfolio_summary,
)

CASES = json.loads((Path(__file__).parent / "fixtures/portfolio.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_portfolio_metrics_and_missing_position_fields_match_original(case):
    assert {
        "correlation": correlation_summary(case["input"]),
        "liquidity": liquidity_summary(case["input"]),
        "holdings": holding_rows(case["input"]),
        "summary": portfolio_summary(case["input"]),
    } == case["expected"]
    assert all(
        holding["shares"] is None and holding["cost_price"] is None and holding["pnl"] is None
        for holding in holding_rows(case["input"])
    )
