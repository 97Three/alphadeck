import json
from copy import deepcopy
from pathlib import Path

import pytest

from alphadeck_platform.data_quality import assess_response
from alphadeck_platform.finance_values import numeric, rounded
from alphadeck_platform.fundamentals import (
    financial_quality,
    financial_quality_summary,
    metric_comparison,
)
from alphadeck_platform.technical import calculate_metrics, ensure_technical_summary

FIXTURE = json.loads((Path(__file__).parent / "fixtures/finance-calculations.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["checks"], ids=lambda case: case["name"])
def test_market_response_quality_and_historical_contract_match_original(case):
    assert assess_response(case["path"], case["payload"], case.get("query")) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["calculations"], ids=lambda case: case["name"])
def test_technical_and_financial_metrics_match_original(case):
    asset = deepcopy(case["input"])
    summary = ensure_technical_summary(asset)
    quality = financial_quality(asset)
    assert {
        "summary": summary,
        "financialQuality": quality,
        "asset": asset,
        "metrics": calculate_metrics(case["input"].get("kline", case["input"].get("history"))),
    } == case["expected"]


def test_financial_comparison_ranking_and_number_conversion_match_original():
    for case in FIXTURE["reports"]:
        assert metric_comparison(case["input"], "2025年报") == case["expected"]
    assets = deepcopy(FIXTURE["qualitySummary"]["input"])
    assert financial_quality_summary(assets) == FIXTURE["qualitySummary"]["expected"]
    assert assets == FIXTURE["qualitySummary"]["assets"]
    for case in FIXTURE["numeric"]:
        assert numeric(case["input"]) == case["expected"]
    assert rounded(-2.5, 0) == -2
    assert rounded(0.49999999999999994, 0) == 0
