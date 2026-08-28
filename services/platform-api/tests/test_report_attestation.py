import json
from pathlib import Path

import pytest

from alphadeck_platform.report_attestation import attest_report

FIXTURES = json.loads((Path(__file__).parent / "fixtures/report-attestation.json").read_text())


@pytest.mark.parametrize("fixture", FIXTURES)
def test_original_complete_report_attestation_and_tamper_detection(fixture):
    assert attest_report(fixture["report"], fixture["options"]) == fixture["expected"]
