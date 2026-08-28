import asyncio
import json
from pathlib import Path

import pytest

from alphadeck_platform.artifact_contracts import definitions, extra_errors
from alphadeck_platform.evidence_quality import assess_evidence
from alphadeck_platform.prefetch_transport import WorkspaceArtifacts
from alphadeck_platform.research_time import ResearchTimeError, resolve_research_time
from alphadeck_platform.validation_dashboard import check_binding, check_charts
from alphadeck_platform.validation_data import check_evidence, check_final_data
from alphadeck_platform.validation_inputs import (
    comparison_symbols,
    empty_screener,
    fetched_symbols,
    inspect_payload,
    planned_symbols,
)

CASES = json.loads((Path(__file__).parent / "fixtures/validation-checks.json").read_text())


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_financial_validation_checks_match_original(case, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "alphadeck_platform.baseline_evidence.timestamp", lambda: "2026-11-03T00:00:00.000Z"
    )

    async def run():
        for relative, content in case["files"].items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        async def authority(operation):
            return await operation(None)

        artifacts = WorkspaceArtifacts(tmp_path, authority)
        actual = {
            "final": check_final_data(tmp_path),
            "binding": check_binding(tmp_path),
            "charts": check_charts(tmp_path),
            "evidence": await check_evidence(artifacts),
        }
        assert actual == case["expected"]

    asyncio.run(run())


@pytest.mark.parametrize("case", CASES["schemas"], ids=lambda case: case["name"])
def test_artifact_json_schemas_accept_same_inputs_as_original(case):
    definition, validator = next(
        (item, validator) for item, validator in definitions() if item["id"] == case["id"]
    )
    value = case.get("input")
    passed = validator.is_valid(value) if "input" in case else False
    if passed and case["id"] == "finance_query_rewrite_contract" and value.get("timeRange"):
        try:
            resolve_research_time(value["timeRange"])
        except ResearchTimeError:
            passed = False
    assert passed == case["passed"]
    if "input" in case:
        assert extra_errors(definition["extraValidate"], value) == case["extra"]


@pytest.mark.parametrize("case", CASES["inputs"])
def test_validation_input_helpers_match_original(case):
    data = case["data"]
    assert {
        "inspection": inspect_payload(data),
        "empty": empty_screener(data),
        "planned": planned_symbols(case["plan"]),
        "fetched": fetched_symbols(data),
        "comparison": comparison_symbols(data),
    } == case["expected"]


@pytest.mark.parametrize("case", CASES["evidence"], ids=lambda case: case["name"])
def test_evidence_quality_matches_original(case):
    assert assess_evidence(case["sources"], case["quality"]) == case["expected"]
