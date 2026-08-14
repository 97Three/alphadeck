import asyncio
import copy
import json
import shutil
from pathlib import Path

import pytest

from alphadeck_platform.benchmark_cli import execute_benchmark, options_from_args
from alphadeck_platform.benchmark_cli import parser as benchmark_parser
from alphadeck_platform.benchmark_runner import BenchmarkRunner
from alphadeck_platform.config import ROOT
from alphadeck_platform.evaluation_ci import check_report, compare_reports, parser
from alphadeck_platform.projects import Projects
from alphadeck_platform.shadow_replay import attest, prepare


@pytest.mark.parametrize(
    "case", json.loads((Path(__file__).parent / "fixtures/evaluation-regression.json").read_text())
)
def test_case_paired_regression_matches_original(case):
    assert compare_reports(case["current"], case["baseline"]) == case["expected"]


@pytest.mark.parametrize(
    "case", json.loads((Path(__file__).parent / "fixtures/shadow-replay.json").read_text())
)
def test_production_replay_privacy_contract_matches_original(case):
    if "error" in case:
        with pytest.raises(ValueError, match=case["error"]):
            prepare(case["input"], case["key"])
    else:
        value = prepare(case["input"], case["key"])
        assert value == case["expected"] and attest(value) == case["attestation"]
        value["userId"] = "must-not-be-persisted"
        assert not attest(value)["passed"]


def test_native_ci_recomputes_a_real_native_report_and_rejects_modified_proof(tmp_path):
    shutil.copytree(ROOT / "benchmarks/alphadeck", tmp_path / "benchmarks/alphadeck")

    async def run():
        runner = BenchmarkRunner(Projects(None, tmp_path / "projects"), None, None, root=tmp_path)

        async def authority(operation):
            return await operation(None)

        runner.authority = authority
        identity = "runtime-registry-deepseek-v4-flash"
        build = {
            "frameworkVersion": "python-1",
            "buildRevision": "controlled-ci-build",
            "gitRevision": "a" * 40,
        }
        report, _ = await execute_benchmark(
            runner,
            options_from_args(benchmark_parser().parse_args(["--case", identity])),
            identity=build,
        )
        args = parser().parse_args(["--case", identity, "--min-average-score", "0"])
        checked = check_report(report, args, root=tmp_path, build=build)
        assert checked["passed"], checked["problems"]
        modified = copy.deepcopy(report)
        modified["metadata"]["provenance"]["casesSha256"] = "0" * 64
        assert not check_report(modified, args, root=tmp_path, build=build)["passed"]
        assert check_report(report, args, root=tmp_path, build=build, baseline=report)["passed"]

    asyncio.run(run())
