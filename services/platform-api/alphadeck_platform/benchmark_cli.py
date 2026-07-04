import argparse
import asyncio
import hashlib
import os
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from .benchmark_runner import BenchmarkRunner
from .build_identity import build_identity
from .config import ROOT, Settings
from .database import Database
from .evaluation_attestation import DEFAULT_THRESHOLDS, attested, execution_summary, quality
from .evaluation_cases import case_type, tags
from .evaluation_cases import coverage as case_coverage
from .evaluation_evaluators import definition
from .evaluation_platform import REPORTS_PATH
from .evaluation_queue import bounded
from .evaluation_reports import skill_snapshot
from .evaluation_scoring import quality_summary
from .evaluation_snapshots import attest_registry, attest_snapshot, payload_sha256, prompt_hash
from .finance_values import js_json
from .generation_state import now
from .llm_config import project_llm_config
from .preview import PreviewManager
from .product_control_evidence import attest_product_controls
from .projects import Projects
from .report_attestation import SCHEMA_VERSION, attest_report
from .scaffold import controlled_path
from .settings import DEFAULT_MODEL, MODEL_ALIASES, normalize_model
from .skill_integrity import read_regular
from .skill_source import strict_json
from .validation_runtime import ProjectValidator
from .workspace import write_atomic


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def parser():
    value = argparse.ArgumentParser(description="AlphaDeck Python benchmark runner")
    value.add_argument("--case", action="append", default=[])
    value.add_argument("--limit", type=int)
    value.add_argument("--keep-projects", action="store_true")
    value.add_argument(
        "--mode", choices=("contract", "e2e"), default=os.getenv("ALPHADECK_EVAL_MODE", "contract")
    )
    value.add_argument("--e2e", action="store_const", dest="mode", const="e2e")
    value.add_argument("--contract", action="store_const", dest="mode", const="contract")
    value.add_argument(
        "--dataset-visibility",
        choices=("public", "hidden", "production_replay"),
        default=os.getenv("ALPHADECK_EVAL_DATASET_VISIBILITY", "public"),
    )
    value.add_argument("--cases-file", default=os.getenv("ALPHADECK_EVAL_CASES_PATH"))
    value.add_argument("--cli", choices=("pi",), default="pi")
    value.add_argument("--model", default=os.getenv("ALPHADECK_EVAL_MODEL", DEFAULT_MODEL))
    value.add_argument("--reasoning-effort", default="")
    value.add_argument("--trigger", default=os.getenv("ALPHADECK_EVAL_TRIGGER", "cli"))
    value.add_argument(
        "--evaluator",
        "--evaluator-id",
        dest="evaluatorId",
        default=os.getenv("ALPHADECK_EVAL_EVALUATOR", "rule-strict"),
    )
    value.add_argument(
        "--concurrency", type=int, default=int(os.getenv("ALPHADECK_EVAL_CONCURRENCY", "1"))
    )
    value.add_argument("--repeat", type=int, default=int(os.getenv("ALPHADECK_EVAL_REPEAT", "1")))
    return value


def options_from_args(arguments):
    options = vars(arguments).copy()
    options.update(
        selectedCases=list(dict.fromkeys(options.pop("case"))),
        keepProjects=options.pop("keep_projects"),
        datasetVisibility=options.pop("dataset_visibility"),
        casesFile=options.pop("cases_file"),
    )
    requested = options["model"].strip().lower()
    if not any(requested in aliases for aliases in MODEL_ALIASES.values()):
        raise ValueError("benchmark 收到未注册的 PI Agent 模型：" + options["model"])
    options["model"], options["reasoningEffort"] = normalize_model(requested), ""
    if not 1 <= options["repeat"] <= 5:
        raise ValueError("--repeat 必须是 1 到 5 的整数")
    options["concurrency"] = bounded(options["concurrency"], 1, 16)
    options["limit"] = options["limit"] if options["limit"] and options["limit"] > 0 else None
    evaluator = definition(options["evaluatorId"])
    if options["mode"] not in evaluator["supportedModes"]:
        raise ValueError(evaluator["id"] + " 不支持 " + options["mode"] + " 模式。")
    return options


def coverage_summary(cases, results):
    value = {
        key: {}
        for key in ("byCapability", "byType", "byTag", "caseLevels", "caseTags", "failedTags")
    }
    value["byLevel"] = {level: {} for level in ("routing", "contract", "live_e2e", "production")}

    def add(bucket, identity, passed):
        item = bucket.setdefault(identity, {"total": 0, "passed": 0, "failed": 0})
        item["total"] += 1
        item["passed" if passed else "failed"] += 1

    for case, result in zip(cases, results, strict=True):
        passed, capability = result["passed"], case.get("capabilityId", "unknown")
        case_tags = tags(case)
        levels = list(
            dict.fromkeys(
                [
                    case_coverage(case),
                    *(
                        ["routing"]
                        if case_type(case) == "renderer_capability_contract"
                        or case.get("selectionExpectations")
                        else []
                    ),
                    *(["live_e2e"] if result.get("executionMode") == "e2e" else []),
                    *(["production"] if case.get("productionSupported") else []),
                ]
            )
        )
        value["caseTags"][case["id"]], value["caseLevels"][case["id"]] = case_tags, levels
        for level in levels:
            add(value["byLevel"][level], capability, passed)
        if "routing" in levels:
            for expectation in case.get("selectionExpectations", []):
                if expectation.get("capabilityId") and expectation["capabilityId"] != capability:
                    add(value["byLevel"]["routing"], expectation["capabilityId"], passed)
        add(value["byCapability"], capability, passed)
        add(value["byType"], case_type(case), passed)
        for tag in case_tags:
            add(value["byTag"], tag, passed)
            if not passed:
                value["failedTags"].setdefault(tag, []).append(case["id"])
    capabilities = [
        "fundamental_analysis",
        "technical_analysis",
        "backtest_review",
        "asset_comparison",
        "portfolio_risk",
        "stock_diagnosis",
    ]
    value["requiredCoverage"] = {
        "capabilities": capabilities,
        "tags": [
            "input:image_attachment",
            "intent:clarification_required",
            "intent:clarification_continuation",
            "runtime:deepseek_v4_flash",
            "validation:repair_plan",
            "data:source_degradation",
            "visual:playwright",
        ],
        "levels": {
            "routing": ["sector_rotation", "strategy_research"],
            "contract": capabilities,
            "live_e2e": [
                "fundamental_analysis",
                "technical_analysis",
                "portfolio_risk",
                "stock_diagnosis",
            ],
        },
    }
    return value


def load_dataset(root, options):
    public = strict_json(
        read_regular(controlled_path(root, "benchmarks/alphadeck/cases.json"), 8388608)
    )
    visibility = options["datasetVisibility"]
    if visibility == "public":
        if options.get("casesFile"):
            raise ValueError("public 评测固定使用仓库 cases.json，不能通过 --cases-file 替换")
        return public
    if options["mode"] != "e2e":
        raise ValueError(visibility + " 评测必须使用真实 e2e 模式")
    path = options.get("casesFile") or os.getenv(
        "ALPHADECK_HIDDEN_EVAL_CASES_PATH"
        if visibility == "hidden"
        else "ALPHADECK_PRODUCTION_REPLAY_CASES_PATH"
    )
    if not path:
        raise ValueError(visibility + " 评测必须通过环境变量或 --cases-file 注入外部数据集")
    target = Path(path).resolve(strict=True)
    if target.is_relative_to(root):
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", target.relative_to(root).as_posix()],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if tracked.returncode == 0:
            raise ValueError(visibility + " 数据集被 Git 跟踪，拒绝执行以防测试集泄漏")
    cases = strict_json(read_regular(target, 8388608))
    registry = strict_json(read_regular(root / "benchmarks/alphadeck/datasets.json", 8388608))
    result = attest_registry(registry, public, {visibility: cases})
    if not result["passed"]:
        raise ValueError("；".join(result["problems"]))
    return cases


async def execute_benchmark(runner, options, *, argv=None, identity=None):
    root, started = runner.root, now()
    all_cases = load_dataset(root, options)
    if not isinstance(all_cases, list) or not all(
        isinstance(case, dict)
        and isinstance(case.get("id"), str)
        and isinstance(case.get("question"), str)
        for case in all_cases
    ):
        raise ValueError("评测数据集必须是包含 id/question 的 case 数组")
    unknown = [
        identity
        for identity in options["selectedCases"]
        if not any(case["id"] == identity for case in all_cases)
    ]
    if unknown:
        raise ValueError("包含未知 benchmark case：" + ", ".join(unknown))
    cases = [
        case
        for case in all_cases
        if not options["selectedCases"] or case["id"] in options["selectedCases"]
    ]
    suite = None
    if options["mode"] == "e2e" and options["datasetVisibility"] == "public":
        suite = strict_json(read_regular(root / "benchmarks/alphadeck/e2e-suite.json", 8388608))
        if not options["selectedCases"]:
            cases = [case for case in cases if case["id"] in suite["caseIds"]]
    if options["limit"]:
        cases = cases[: options["limit"]]
    if not cases:
        raise ValueError("没有匹配的 benchmark case。")
    registry = strict_json(read_regular(root / "benchmarks/alphadeck/datasets.json", 8388608))
    manifest = strict_json(
        read_regular(root / "benchmarks/alphadeck/snapshot-manifest.json", 8388608)
    )
    chosen = [
        item
        for item in manifest.get("snapshots", [])
        if any(case["id"] == item["caseId"] for case in cases)
    ]
    for snapshot in chosen:
        payload = strict_json(
            read_regular(
                controlled_path(root / "benchmarks/alphadeck", snapshot["fixturePath"]), 8388608
            )
        )
        checked = attest_snapshot(snapshot, payload, expected_case=snapshot["caseId"])
        if not checked["passed"]:
            raise ValueError("；".join(checked["problems"]))
    snapshots = [
        {key: item[key] for key in ("caseId", "id", "asOf", "payloadSha256")} for item in chosen
    ]
    config, build = project_llm_config(options["model"]), identity or build_identity()
    formal_suite = bool(suite and not options["selectedCases"] and not options["limit"])
    controls = None
    if formal_suite:
        control_started = now()
        control_results = []
        for control_id in suite["productControlCaseIds"]:
            control_case = next((case for case in all_cases if case["id"] == control_id), None)
            if control_case is None:
                raise ValueError("E2E suite 引用了未知产品控制 case：" + control_id)
            control_results.append(
                await runner.run_case(
                    control_case, {**options, "mode": "product-control", "repeat": 1}
                )
            )
        runtime_files = sorted(
            {path for scenario in suite["scenarios"].values() for path in scenario["runtimeTests"]}
        )
        for path in runtime_files:
            if (
                not path.startswith("services/platform-api/tests/")
                or not path.endswith(".py")
                or not controlled_path(root, path).is_file()
            ):
                raise ValueError("E2E suite 的原生运行时测试不存在：" + path)
        controls = {
            "schemaVersion": 1,
            "suiteId": suite["id"],
            "suiteSchemaVersion": suite["schemaVersion"],
            **{key: build[key] for key in ("frameworkVersion", "buildRevision", "gitRevision")},
            "startedAt": control_started,
            "finishedAt": now(),
            "caseIds": suite["productControlCaseIds"],
            "runtimeTestFiles": runtime_files,
            "passed": all(result["passed"] for result in control_results),
            "results": control_results,
        }
        controls["attestation"] = {
            "schemaVersion": 1,
            "verifiedAt": now(),
            **attest_product_controls(controls, suite, build),
        }
    expected_runtime = {"provider": config["provider"], "model": config["model"]}
    semaphore = asyncio.Semaphore(options["concurrency"])

    async def run(case):
        async with semaphore:
            attempts = []
            for index in range(options["repeat"]):
                print(
                    f"[QuantBenchmark:{options['mode']}] {case['id']} attempt={index + 1}",
                    flush=True,
                )
                attempts.append(await runner.run_case(case, options))
            primary = deepcopy(attempts[0])
            passed_count = sum(attempt["passed"] for attempt in attempts)
            primary["durationMs"] = sum(attempt["durationMs"] for attempt in attempts)
            primary["stability"] = {
                "passed": passed_count == len(attempts),
                "repeatCount": len(attempts),
                "passedAttempts": passed_count,
                "passRate": int(passed_count / len(attempts) * 100 + 0.5),
                "flaky": 0 < passed_count < len(attempts),
                "attempts": [
                    {
                        "attempt": index + 1,
                        "passed": value["passed"],
                        "firstPassPassed": value["firstPassPassed"],
                        "score": value["score"],
                        "durationMs": value["durationMs"],
                        "repairAttempts": value.get("repairAttempts", 0),
                        "projectId": value.get("projectId"),
                        "projectPath": value.get("projectPath"),
                        "requestId": value.get("requestId"),
                        "failures": value.get("failures", []),
                        "agentAttested": attested(value, expected_runtime)
                        if options["mode"] == "e2e"
                        else None,
                        **({"evidence": deepcopy(value)} if options["mode"] == "e2e" else {}),
                    }
                    for index, value in enumerate(attempts)
                ],
            }
            return primary

    results = await asyncio.gather(*(run(case) for case in cases))
    quality_summary_result = quality_summary(results, options["repeat"])
    execution = execution_summary(results, expected_runtime)
    e2e = quality(results) if options["mode"] == "e2e" else None
    finished = now()
    queue_id, token, report_id = (
        os.getenv(key, "")
        for key in (
            "ALPHADECK_EVAL_QUEUE_ID",
            "ALPHADECK_EVAL_LEASE_TOKEN",
            "ALPHADECK_EVAL_REPORT_ID",
        )
    )
    if report_id and (not queue_id or not token or not re.fullmatch(r"report-\d+", report_id)):
        raise ValueError("Worker report identity requires an owned queue lease")
    report_id = report_id or "report-" + str(uuid4().int)
    evaluator = definition(options["evaluatorId"])
    command = list(argv or [])
    if options["datasetVisibility"] != "public":
        command = [
            "--cases-file=[external-dataset]"
            if item.startswith("--cases-file=")
            else "[external-dataset]"
            if index and command[index - 1] == "--cases-file"
            else item
            for index, item in enumerate(command)
        ]
        for value, case in zip(results, cases, strict=True):
            redacted = "[redacted:" + prompt_hash(case["question"]) + "]"
            value["question"] = redacted
            for attempt in value["stability"]["attempts"]:
                if "evidence" in attempt:
                    attempt["evidence"]["question"] = redacted
    selected = {
        key: options[key]
        for key in ("selectedCases", "limit", "keepProjects", "concurrency", "repeat")
    }
    provenance = {
        "gitCommit": build["gitRevision"],
        "gitRevision": build["gitRevision"],
        "buildRevision": build["buildRevision"],
        "frameworkVersion": build["frameworkVersion"],
        "casesSha256": digest(js_json(cases)),
        "promptsSha256": digest("\n".join(case["question"] for case in cases)),
        "datasetRegistrySha256": payload_sha256(registry),
        "snapshotManifestSha256": payload_sha256(manifest),
    }
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": finished,
        "metadata": {
            **({"queue": {"id": queue_id, "leaseToken": token}} if queue_id else {}),
            "trigger": options["trigger"],
            "startedAt": started,
            "finishedAt": finished,
            "command": command,
            "evaluator": {
                "id": evaluator["id"],
                "version": evaluator["version"],
                "rubricVersion": evaluator["rubricVersion"],
                "concurrency": options["concurrency"],
            },
            "runtime": {
                "cli": "pi",
                "provider": config["provider"] if options["mode"] == "e2e" else None,
                "model": options["model"] if options["mode"] == "e2e" else None,
                "configuredModel": options["model"],
                "agentExecuted": options["mode"] == "e2e" and execution["agentExecuted"],
                "executedCaseCount": execution["executedCaseCount"],
                "unattestedCaseIds": execution["unattestedCaseIds"]
                if options["mode"] == "e2e"
                else [],
                "frameworkVersion": build["frameworkVersion"],
                "buildRevision": build["buildRevision"],
                "reasoningEffort": None,
            },
            "suite": {
                "mode": options["mode"],
                "label": options["model"] + " 真实生成 E2E"
                if options["mode"] == "e2e"
                else "确定性产物契约",
                "executionClass": "live_mission_e2e"
                if options["mode"] == "e2e"
                else "deterministic_contract",
                **(
                    {
                        "id": suite["id"],
                        "schemaVersion": suite["schemaVersion"],
                        "productControlCaseIds": suite["productControlCaseIds"],
                        "scenarios": suite["scenarios"],
                    }
                    if suite
                    else {}
                ),
            },
            "dataset": {
                "schemaVersion": 1,
                "visibility": options["datasetVisibility"],
                "promptsRedacted": options["datasetVisibility"] != "public",
                "sourceIdentitySha256": digest(
                    "benchmarks/alphadeck/cases.json"
                    if options["datasetVisibility"] == "public"
                    else options["datasetVisibility"]
                ),
            },
            "provenance": provenance,
            "dataSnapshots": {
                "schemaVersion": 1,
                "manifestId": manifest.get("id"),
                "manifestVersion": manifest.get("version"),
                "selected": snapshots,
            },
            "e2eQuality": e2e,
            "selection": {**selected, "caseCount": len(cases)},
            "retention": {
                "databaseEvidenceRetained": options["mode"] == "e2e",
                "workspaceRetained": options["mode"] == "e2e" or options["keepProjects"],
            },
            **({"releaseControls": controls} if controls else {}),
            "skillLockSnapshot": skill_snapshot(
                strict_json(read_regular(ROOT / ".pi/skills.lock.json", 8388608))
            ),
        },
        "passed": all(value["passed"] for value in results)
        and quality_summary_result["stability"]["passRate"] == 100
        and (e2e["passed"] if e2e else True),
        "total": len(results),
        "passedCount": sum(value["passed"] for value in results),
        "failedCount": sum(not value["passed"] for value in results),
        "coverage": coverage_summary(cases, results),
        "qualitySummary": quality_summary_result,
        "e2eQuality": e2e,
        "results": results,
    }
    checked = attest_report(
        report,
        {
            "mode": options["mode"],
            "expectedCaseIds": [case["id"] for case in cases],
            "expectedCasesSha256": provenance["casesSha256"],
            "expectedPromptsSha256": provenance["promptsSha256"],
            "expectedDatasetRegistrySha256": provenance["datasetRegistrySha256"],
            "expectedSnapshotManifestSha256": provenance["snapshotManifestSha256"],
            "expectedDataSnapshots": snapshots,
            "expectedDatasetVisibility": options["datasetVisibility"],
            "expectedRuntimeProvider": config["provider"],
            "expectedRuntimeModel": options["model"],
            "expectedResultQuestions": {
                case["id"]: case["question"]
                if options["datasetVisibility"] == "public"
                else "[redacted:" + prompt_hash(case["question"]) + "]"
                for case in cases
            },
            "frameworkVersion": build["frameworkVersion"],
            "buildRevision": build["buildRevision"],
            "gitRevision": build["gitRevision"],
            "qualityThresholds": DEFAULT_THRESHOLDS,
            "now": finished,
        },
    )
    report["attestation"] = {"schemaVersion": 1, "verifiedAt": finished, **checked}
    if not checked["passed"]:
        report["passed"] = False
    report["releasePassed"] = report["passed"] and (
        controls["attestation"]["passed"] if controls else True
    )

    async def save(_tx):
        return write_atomic(
            root,
            REPORTS_PATH + "/" + report_id + ".json",
            (js_json(report, indent=2) + "\n").encode(),
        )

    path = await runner.authority(save)
    return report, path


async def main(argv=None):
    options = options_from_args(parser().parse_args(argv))
    settings = Settings.from_env()
    root = Path(os.getenv("ALPHADECK_EVAL_ROOT", str(ROOT))).resolve(strict=True)
    projects = Projects(Database(settings.database_url), settings.projects_dir)
    previews = PreviewManager(projects)
    runner = BenchmarkRunner(projects, previews, ProjectValidator(previews), root=root)
    try:
        report, path = await execute_benchmark(runner, options, argv=argv or sys.argv[1:])
        print(
            f"[QuantBenchmark] {'passed' if report['passed'] else 'failed'} report={path.name} total={report['total']}",
            flush=True,
        )
        return 0 if report["releasePassed"] else 1
    finally:
        await previews.cleanup_all()
        if options["mode"] == "contract" and not options["keepProjects"]:
            for project in runner.created:
                await projects.delete(project)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (ValueError, OSError) as error:
        print("[QuantBenchmark] " + str(error), file=sys.stderr)
        raise SystemExit(1) from error
