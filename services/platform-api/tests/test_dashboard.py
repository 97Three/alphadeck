import copy
import json
from pathlib import Path

import pytest

from alphadeck_platform.dashboard import (
    comparison_summary,
    conclusion,
    selection_ranking,
    trading_plan,
)
from alphadeck_platform.fundamentals import financial_quality_summary
from alphadeck_platform.prefetch_planning import (
    broad_screener,
    history_limit,
    planned_symbols,
    screener_limit,
    screener_mode,
    screener_trade_date,
    trading_plan_intent,
)
from alphadeck_platform.visualization import serialize_template

FIXTURES = Path(__file__).parent / "fixtures"
VISUAL = json.loads((FIXTURES / "visualization.json").read_text())
CASES = json.loads((FIXTURES / "dashboard.json").read_text())
PLANNING = json.loads((FIXTURES / "prefetch-planning.json").read_text())


@pytest.mark.parametrize("case", VISUAL)
def test_visualization_selection_matches_original(case):
    assert serialize_template(case["id"], case["context"]) == case["expected"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_dashboard_summary_matches_original(case):
    assets = copy.deepcopy(case["input"])
    comparison = comparison_summary(assets)
    selection = selection_ranking(comparison, assets)
    actual = {
        "comparison": comparison,
        "selection": selection,
        "trading": trading_plan(assets, selection),
        "conclusion": conclusion(comparison, selection, financial_quality_summary(assets)),
    }
    assert actual == case["expected"]


@pytest.mark.parametrize("case", PLANNING["plans"])
def test_prefetch_plan_contract(case):
    plan = case["input"]
    assert {
        "symbols": planned_symbols(plan),
        "historyLimit": history_limit(plan),
        "broad": broad_screener(plan),
    } == case["expected"]


@pytest.mark.parametrize("case", PLANNING["questions"])
def test_prefetch_question_contract(case):
    value = case["input"]
    assert {
        "intent": trading_plan_intent(value),
        "mode": screener_mode(value),
        "limit": screener_limit(value),
        "tradeDate": screener_trade_date(value),
    } == case["expected"]
