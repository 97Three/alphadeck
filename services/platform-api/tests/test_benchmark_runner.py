import asyncio
import json
import shutil

import pytest

from alphadeck_platform.benchmark_cli import execute_benchmark, options_from_args, parser
from alphadeck_platform.benchmark_runner import BenchmarkRunner
from alphadeck_platform.config import ROOT
from alphadeck_platform.projects import Projects


@pytest.mark.parametrize(
    "identity",
    [
        "runtime-registry-deepseek-v4-flash",
        "validation-repair-contract",
        "dashboard-renderer-capability-contract",
    ],
)
def test_native_structural_benchmark_uses_production_contracts(tmp_path, identity):
    async def run():
        runner = BenchmarkRunner(Projects(None, tmp_path / "projects"), None, None)
        cases = json.loads((ROOT / "benchmarks/alphadeck/cases.json").read_text())
        case = next(case for case in cases if case["id"] == identity)
        options = options_from_args(parser().parse_args(["--case", identity]))
        result = await runner.run_case(case, options)
        assert result["passed"], result["failures"]
        assert result["evaluation"]["hardGatePassed"]
        assert result["firstPassPassed"] and result["finalPassed"]

    asyncio.run(run())


def test_complete_native_report_attests_repeat_stability_and_is_saved_atomically(tmp_path):
    shutil.copytree(ROOT / "benchmarks/alphadeck", tmp_path / "benchmarks/alphadeck")

    async def run():
        runner = BenchmarkRunner(Projects(None, tmp_path / "projects"), None, None, root=tmp_path)

        async def authority(operation):
            return await operation(None)

        runner.authority = authority
        options = options_from_args(
            parser().parse_args(
                [
                    "--case",
                    "runtime-registry-deepseek-v4-flash",
                    "--repeat",
                    "3",
                    "--concurrency",
                    "2",
                ]
            )
        )
        identity = {
            "frameworkVersion": "python-1",
            "buildRevision": "controlled-build",
            "gitRevision": "a" * 40,
        }
        report, path = await execute_benchmark(runner, options, identity=identity)
        assert report["passed"] and report["attestation"]["passed"], report["attestation"][
            "problems"
        ]
        assert report["qualitySummary"]["stability"]["attemptCount"] == 3
        assert len(report["results"][0]["stability"]["attempts"]) == 3
        assert json.loads(path.read_text()) == report
        assert report["metadata"]["runtime"]["agentExecuted"] is False

    asyncio.run(run())
