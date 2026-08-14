import json
from pathlib import Path

from alphadeck_platform.evaluation_mutations import run_mutations


def test_native_mutation_suite_matches_all_original_detectors():
    expected = json.loads(
        (Path(__file__).parent / "fixtures/evaluation-mutations.json").read_text()
    )
    actual = run_mutations(created_at="2026-11-03T00:00:00.000Z")
    assert actual == expected
    assert actual["baselinePassed"] and actual["killed"] == actual["total"] == 25
