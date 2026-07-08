import argparse
import asyncio
import json
import os
from pathlib import Path

from .config import ROOT, Settings, load_environment
from .database import Database
from .evaluation_diagnostics import calibration
from .evaluation_queue import EvaluationQueue
from .evaluation_snapshots import attest_registry, attest_snapshot
from .skill_integrity import read_regular
from .skill_source import strict_json


async def main(args):
    load_environment()
    if args.command == "schedule":
        queue = EvaluationQueue(Database(Settings.from_env().database_url))
        result = await queue.check_schedule()
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    if args.command == "judge":
        path = (
            args.input
            or os.getenv("ALPHADECK_EVAL_JUDGE_CALIBRATION_PATH")
            or str(ROOT / "benchmarks/alphadeck/judge-calibration.contract.json")
        )
        dataset = strict_json(read_regular(Path(path).resolve(strict=True), 8388608))
        if dataset.get("schemaVersion") != 1 or not isinstance(dataset.get("samples"), list):
            raise ValueError("Judge 校准集必须使用 schemaVersion 1 并包含 samples")
        if (
            os.getenv("ALPHADECK_REQUIRE_PRODUCTION_JUDGE_CALIBRATION") == "1"
            and dataset.get("datasetKind") != "human_blind_calibration"
        ):
            raise ValueError("发布门要求外部 human_blind_calibration，不能使用 pipeline_contract")
        result = calibration(
            dataset["samples"],
            min_agreement=args.min_agreement_rate,
            min_kappa=args.min_kappa,
            max_error=args.max_score_mae,
            independent=os.getenv("ALPHADECK_REQUIRE_INDEPENDENT_JUDGE") == "1"
            or dataset.get("datasetKind") == "human_blind_calibration",
        )
    else:
        base = ROOT / "benchmarks/alphadeck"
        registry = strict_json(read_regular(base / "datasets.json", 8388608))
        cases = strict_json(read_regular(base / "cases.json", 8388608))
        external = {}
        for definition in registry["datasets"]:
            if definition.get("visibility") in ("hidden", "production_replay") and (
                path := os.getenv(definition.get("pathEnv", ""))
            ):
                external[definition["visibility"]] = strict_json(
                    read_regular(Path(path).resolve(strict=True), 8388608)
                )
        result = attest_registry(registry, cases, external)
        if args.command == "snapshots":
            manifest = strict_json(read_regular(base / "snapshot-manifest.json", 8388608))
            for snapshot in manifest["snapshots"]:
                path = base / snapshot["fixturePath"]
                if not path.resolve(strict=True).is_relative_to(base):
                    raise ValueError("Snapshot path leaves the repository")
                result["problems"].extend(
                    attest_snapshot(
                        snapshot,
                        strict_json(read_regular(path, 8388608)),
                        expected_case=snapshot["caseId"],
                    )["problems"]
                )
            result["passed"] = not result["problems"]
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("datasets", "snapshots", "judge", "schedule"))
    parser.add_argument("--input")
    parser.add_argument("--min-agreement-rate", type=float, default=80)
    parser.add_argument("--min-kappa", type=float, default=0.6)
    parser.add_argument("--max-score-mae", type=float, default=15)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
