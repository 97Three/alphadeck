import json
from pathlib import Path

import pytest

from alphadeck_platform.config import ROOT
from alphadeck_platform.evaluation_reports import normalize_run

FIXTURES = json.loads((Path(__file__).parent / "fixtures/evaluation-reports.json").read_text())


@pytest.mark.parametrize("fixture", FIXTURES)
def test_original_evaluation_report_contract(fixture):
    assert (
        normalize_run(ROOT / fixture["path"], fixture["mtime"], fixture["report"], [])
        == fixture["expected"]
    )
