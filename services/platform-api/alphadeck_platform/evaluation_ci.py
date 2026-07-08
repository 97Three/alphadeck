"""Native release gate: recompute evidence, stability, quality and paired regressions."""

import argparse
import asyncio
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .benchmark_cli import digest, load_dataset
from .benchmark_cli import main as run_benchmark
from .build_identity import build_identity
from .config import ROOT, load_environment
from .evaluation_attestation import DEFAULT_THRESHOLDS
from .evaluation_queue import date
from .evaluation_scoring import quality_summary, record, result_score
from .evaluation_snapshots import payload_sha256, prompt_hash
from .finance_values import js_json, js_string
from .llm_config import project_llm_config
from .product_control_evidence import attest_product_controls
from .report_attestation import attest_report
from .skill_integrity import read_regular
from .skill_source import strict_json


def compare_reports(current, baseline):
    current = record(current)
    baseline = record(baseline)
    current_results = [record(item) for item in current.get("results", [])]
    baseline_results = [record(item) for item in baseline.get("results", [])]
    previous = {item["id"]: item for item in baseline_results if isinstance(item.get("id"), str)}
    cases = []
    for item in current_results:
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or identity not in previous:
            continue
        old = previous[identity]
        cases.append(
            {
                "id": identity,
                "baselineScore": result_score(old),
                "currentScore": result_score(item),
                "scoreDelta": result_score(item) - result_score(old),
                "baselinePassed": old.get("passed") is True,
                "currentPassed": item.get("passed") is True,
                "baselineFirstPassPassed": old.get("firstPassPassed") is True,
                "currentFirstPassPassed": item.get("firstPassPassed") is True,
            }
        )
    return {
        "matchedCaseCount": len(cases),
        "currentOnlyCaseIds": [
            js_string(item.get("id") or "")
            for item in current_results
            if item.get("id") and item.get("id") not in previous
        ],
        "baselineOnlyCaseIds": [
            js_string(item.get("id") or "")
            for item in baseline_results
            if item.get("id")
            and not any(value.get("id") == item["id"] for value in current_results)
        ],
        "scoreDelta": math.floor(sum(item["scoreDelta"] for item in cases) / len(cases) + 0.5)
        if cases
        else 0,
        "passRegressions": [
            item["id"] for item in cases if item["baselinePassed"] and not item["currentPassed"]
        ],
        "firstPassRegressions": [
            item["id"]
            for item in cases
            if item["baselineFirstPassPassed"] and not item["currentFirstPassPassed"]
        ],
        "cases": cases,
    }


def expected_cases(args, root=ROOT):
    cases = load_dataset(
        root,
        {
            "datasetVisibility": args.dataset_visibility,
            "mode": args.mode,
            "casesFile": args.cases_file,
        },
    )
    suite = (
        strict_json(read_regular(root / "benchmarks/alphadeck/e2e-suite.json", 8388608))
        if args.mode == "e2e" and args.dataset_visibility == "public"
        else None
    )
    selected = set(args.case)
    if selected - {case["id"] for case in cases}:
        raise ValueError("CI selection contains an unknown case")
    if selected:
        cases = [case for case in cases if case["id"] in selected]
    elif suite:
        cases = [case for case in cases if case["id"] in suite["caseIds"]]
    return cases, suite


def check_report(report, args, *, root=ROOT, build=None, baseline=None):
    cases, suite = expected_cases(args, root)
    build = build or build_identity()
    registry = strict_json(read_regular(root / "benchmarks/alphadeck/datasets.json", 8388608))
    manifest = strict_json(
        read_regular(root / "benchmarks/alphadeck/snapshot-manifest.json", 8388608)
    )
    snapshots = [
        {key: item[key] for key in ("caseId", "id", "asOf", "payloadSha256")}
        for item in manifest["snapshots"]
        if item["caseId"] in {case["id"] for case in cases}
    ]
    llm = project_llm_config(args.model)
    checked = attest_report(
        report,
        {
            "mode": args.mode,
            "expectedCaseIds": [case["id"] for case in cases],
            "expectedCasesSha256": digest(js_json(cases)),
            "expectedPromptsSha256": digest("\n".join(case["question"] for case in cases)),
            "expectedDatasetRegistrySha256": payload_sha256(registry),
            "expectedSnapshotManifestSha256": payload_sha256(manifest),
            "expectedDataSnapshots": snapshots,
            "expectedDatasetVisibility": args.dataset_visibility,
            "expectedRuntimeProvider": llm["provider"],
            "expectedRuntimeModel": llm["model"],
            "expectedResultQuestions": {
                case["id"]: case["question"]
                if args.dataset_visibility == "public"
                else "[redacted:" + prompt_hash(case["question"]) + "]"
                for case in cases
            },
            **build,
            "qualityThresholds": {
                "maxTurnsPerCase": args.max_turns_per_case,
                "maxCacheMissInputTokensPerCase": args.max_cache_miss_input_tokens_per_case,
                "maxUnexpectedToolFailures": args.max_unexpected_tool_failures,
            },
        },
    )
    problems = list(checked["problems"])
    meta = record(report.get("metadata"))
    if suite and not args.case:
        controls = attest_product_controls(meta.get("releaseControls"), suite, build)
        problems.extend(controls["problems"])
        if report.get("releasePassed") is not (report.get("passed") is True and controls["passed"]):
            problems.append("releasePassed 与重算的发布控制结果不一致")
    results = report.get("results", [])
    summary = quality_summary(results, record(meta.get("selection")).get("repeat", 1))
    total = len(results)
    pass_rate = (
        math.floor(sum(record(item).get("passed") is True for item in results) / total * 100 + 0.5)
        if total
        else 0
    )
    average = summary["averageScore"]
    created = date(report.get("createdAt"))
    age = (datetime.now(UTC) - created).total_seconds() / 3600 if created else float("inf")
    if created is None or args.max_age_hours > 0 and age > args.max_age_hours or age < -(5 / 60):
        problems.append("评测报告超过允许时间窗口")
    gates = (
        (pass_rate < args.min_pass_rate, "通过率低于要求"),
        (average < args.min_average_score, "平均分低于要求"),
        (summary["firstPassRate"] < args.min_first_pass_rate, "首次通过率低于要求"),
        (summary["stability"]["passRate"] < args.min_stability_rate, "重复运行稳定率低于要求"),
        (
            summary["stability"]["confidence95"]["lower"] < args.min_stability_confidence_lower,
            "稳定率置信下界低于要求",
        ),
        (
            summary["stability"]["scoreStdDev"]["max"]["value"] > args.max_score_standard_deviation,
            "评分标准差超过要求",
        ),
        (summary["repairRate"] > args.max_repair_rate, "修复率超过要求"),
        (
            os.getenv("ALPHADECK_CI_ALLOW_FAILED") != "1"
            and any(record(item).get("passed") is not True for item in results),
            "存在失败 case",
        ),
    )
    problems.extend(message for failed, message in gates if failed)
    regression = None
    if baseline is not None:
        old_meta = record(baseline.get("metadata"))
        if (
            any(record(old_meta.get(key)) != record(meta.get(key)) for key in ("evaluator",))
            or any(
                record(old_meta.get("provenance")).get(key)
                != record(meta.get("provenance")).get(key)
                for key in ("casesSha256", "datasetRegistrySha256", "snapshotManifestSha256")
            )
            or baseline.get("schemaVersion") != report.get("schemaVersion")
            or record(old_meta.get("suite")).get("mode") != args.mode
            or record(old_meta.get("dataset")).get("visibility") != args.dataset_visibility
        ):
            problems.append("baseline 与当前报告的评测合同不一致")
        else:
            regression = compare_reports(report, baseline)
            if regression["matchedCaseCount"] != total:
                problems.append("baseline case 集不完整")
            if (
                regression["scoreDelta"] < -args.max_score_regression
                or regression["passRegressions"]
                or regression["firstPassRegressions"]
            ):
                problems.append("对比 baseline 出现退化")
    return {
        "passed": not problems,
        "problems": problems,
        "passRate": pass_rate,
        "averageScore": average,
        "qualitySummary": summary,
        "regression": regression,
    }


def parser():
    result = argparse.ArgumentParser()
    result.add_argument(
        "--mode", choices=("contract", "e2e"), default=os.getenv("ALPHADECK_EVAL_MODE", "contract")
    )
    result.add_argument(
        "--dataset-visibility",
        choices=("public", "hidden", "production_replay"),
        default=os.getenv("ALPHADECK_EVAL_DATASET_VISIBILITY", "public"),
    )
    result.add_argument("--report", default=os.getenv("ALPHADECK_EVAL_REPORT"))
    result.add_argument("--baseline-report", default=os.getenv("ALPHADECK_EVAL_BASELINE_REPORT"))
    result.add_argument("--cases-file", default=os.getenv("ALPHADECK_EVAL_CASES_PATH"))
    result.add_argument("--case", action="append", default=[])
    result.add_argument(
        "--model", default=os.getenv("ALPHADECK_EVAL_MODEL", "local_qwen:qwen3.5-9b-q5km")
    )
    result.add_argument("--run-if-missing", action="store_true")
    for flag, fallback in (
        ("min-pass-rate", 100),
        ("min-average-score", 85),
        ("min-first-pass-rate", 0),
        ("min-stability-rate", 100),
        ("min-stability-confidence-lower", 0),
        ("max-score-standard-deviation", 100),
        ("max-repair-rate", 100),
        ("max-score-regression", 0),
        ("max-age-hours", 168),
    ):
        result.add_argument(
            "--" + flag,
            type=float,
            default=float(
                os.getenv("ALPHADECK_CI_" + flag.replace("-", "_").upper(), str(fallback))
            ),
        )
    for flag, key in (
        ("max-turns-per-case", "maxTurnsPerCase"),
        ("max-cache-miss-input-tokens-per-case", "maxCacheMissInputTokensPerCase"),
        ("max-unexpected-tool-failures", "maxUnexpectedToolFailures"),
    ):
        result.add_argument("--" + flag, type=int, default=DEFAULT_THRESHOLDS[key])
    return result


async def main(argv=None):
    load_environment()
    args = parser().parse_args(argv)
    paths = (
        [Path(args.report).resolve(strict=True)]
        if args.report
        else sorted(
            (ROOT / "tmp/alphadeck-benchmark-reports").glob("report-*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    )
    report = None
    for path in paths:
        candidate = strict_json(read_regular(path, 33554432))
        meta = record(candidate.get("metadata"))
        if (
            args.report
            or record(meta.get("suite")).get("mode") == args.mode
            and record(meta.get("dataset")).get("visibility") == args.dataset_visibility
        ):
            report = candidate
            break
    if report is None and args.run_if_missing:
        arguments = [
            "--mode",
            args.mode,
            "--dataset-visibility",
            args.dataset_visibility,
            "--model",
            args.model,
        ]
        for identity in args.case:
            arguments.extend(("--case", identity))
        if args.cases_file:
            arguments.extend(("--cases-file", args.cases_file))
        code = await run_benchmark(arguments)
        return (
            code
            if code
            else await main(
                [
                    argument
                    for argument in (sys.argv[1:] if argv is None else argv)
                    if argument != "--run-if-missing"
                ]
            )
        )
    if report is None:
        raise ValueError(
            "没有匹配的评测报告，请先运行 benchmark:quant:contract 或使用 --run-if-missing"
        )
    baseline = (
        strict_json(read_regular(Path(args.baseline_report).resolve(strict=True), 33554432))
        if args.baseline_report
        else None
    )
    result = check_report(report, args, baseline=baseline)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
