import json
from datetime import datetime
from pathlib import Path

import pytest

from alphadeck_platform.evaluation_snapshots import (
    attest_registry,
    attest_snapshot,
    canonical_json,
    payload_sha256,
    prompt_hash,
)

CASES = json.loads((Path(__file__).parent / "fixtures/evaluation-snapshots.json").read_text())


@pytest.mark.parametrize("case", CASES["canonical"])
def test_locale_sensitive_snapshot_hashes_match_original(case):
    assert canonical_json(case["input"], locale=case["locale"]) == case["expected"]
    assert payload_sha256(case["input"], locale=case["locale"]) == case["sha256"]


@pytest.mark.parametrize("case", CASES["snapshotCases"])
def test_snapshot_attestations_detect_tampering_and_future_leakage(case):
    assert (
        attest_snapshot(
            case["snapshot"],
            case["payload"],
            expected_case=case["expectedCase"],
            now=datetime.fromisoformat(case["now"].replace("Z", "+00:00")),
        )
        == case["expected"]
    )


@pytest.mark.parametrize("case", CASES["registries"])
def test_hidden_datasets_do_not_overlap_or_leak_into_public_registry(case):
    input = case["input"]
    assert (
        attest_registry(input["registry"], input["publicCases"], input.get("externalDatasets"))
        == case["expected"]
    )


@pytest.mark.parametrize("case", CASES["hashes"])
def test_normalized_prompt_hash_matches_original(case):
    assert prompt_hash(case["input"]) == case["expected"]


def test_actual_repository_snapshot_files_are_attested_by_native_cli(capsys):
    import argparse
    import asyncio

    from alphadeck_platform.evaluation_checks import main

    assert asyncio.run(main(argparse.Namespace(command="snapshots"))) == 0
    assert json.loads(capsys.readouterr().out)["passed"]


def test_benchmark_reads_selected_snapshots_before_case_execution():
    import asyncio

    from alphadeck_platform.benchmark_cli import execute_benchmark, options_from_args, parser
    from alphadeck_platform.config import ROOT

    class ReachedCase(Exception):
        pass

    class Runner:
        root = ROOT

        async def run_case(self, *args, **kwargs):
            raise ReachedCase

    with pytest.raises(ReachedCase):
        asyncio.run(
            execute_benchmark(
                Runner(),
                options_from_args(parser().parse_args(["--case", "stock-fundamental-maotai"])),
            )
        )
