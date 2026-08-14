import json
from pathlib import Path

import pytest

from alphadeck_platform.evaluation_attestation import attested, execution_summary, quality

FIXTURES = json.loads((Path(__file__).parent / "fixtures/evaluation-attestation.json").read_text())


@pytest.mark.parametrize("fixture", FIXTURES)
def test_original_e2e_run_receipt_usage_and_tool_attestation(fixture):
    assert attested(fixture["value"], framework=fixture["framework"]) == fixture["attested"]
    assert (
        execution_summary([fixture["value"]], framework=fixture["framework"]) == fixture["summary"]
    )
    assert quality([fixture["value"]]) == fixture["quality"]
