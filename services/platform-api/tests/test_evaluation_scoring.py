import json
from pathlib import Path

import pytest

from alphadeck_platform.evaluation_scoring import (
    deterministic_gate,
    deviation,
    quality_summary,
    result_score,
    weighted_score,
    wilson_interval,
)

CASES = json.loads((Path(__file__).parent / "fixtures/evaluation-scoring.json").read_text())


@pytest.mark.parametrize("case", CASES["scores"])
def test_result_scores_match_original(case):
    assert result_score(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["qualities"])
def test_quality_stability_and_percentiles_match_original(case):
    assert quality_summary(case["input"], case["repeatCount"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["weighted"])
def test_weighted_scores_match_original(case):
    assert weighted_score(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["intervals"])
def test_small_sample_confidence_intervals_match_original(case):
    assert wilson_interval(case["passed"], case["total"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["gates"])
def test_deterministic_gates_reject_false_positive_aggregate_reports(case):
    assert deterministic_gate(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["deviations"])
def test_repeat_score_deviation_matches_original(case):
    assert deviation(case["input"]) == case["expected"]
