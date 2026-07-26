"""Recompute release evidence; a report's own 'passed' field is never authoritative."""

import math
from datetime import UTC, datetime, timedelta

from .evaluation_attestation import attested, execution_summary, quality
from .evaluation_diagnostics import equal, trace_diagnostics
from .evaluation_evaluators import is_current
from .evaluation_queue import date
from .evaluation_reports import CATALOG, records, text
from .evaluation_scoring import deterministic_gate, number, quality_summary, record

SCHEMA_VERSION = 6


def safe_integer(value):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and int(value) == value
        and abs(value) <= 9007199254740991
    )


def duplicates(values):
    seen, repeated = set(), []
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def parsed(value):
    try:
        return date(value) if isinstance(value, str) else None
    except (ValueError, TypeError):
        return None


def attest_report(value, options):
    report = record(value)
    metadata = record(report.get("metadata"))
    runtime, suite, provenance, retention, evaluator, selection, dataset = (
        record(metadata.get(key))
        for key in (
            "runtime",
            "suite",
            "provenance",
            "retention",
            "evaluator",
            "selection",
            "dataset",
        )
    )
    results, problems = records(report.get("results")), []
    mode = options["mode"]
    expected_class = "live_mission_e2e" if mode == "e2e" else "deterministic_contract"
    expected_runtime = {
        "provider": options.get("expectedRuntimeProvider", "openai"),
        "model": options.get("expectedRuntimeModel", CATALOG["defaultModel"]),
    }

    def fail(condition, message):
        if condition:
            problems.append(message)

    schema = report.get("schemaVersion")
    fail(
        not safe_integer(schema) or schema != SCHEMA_VERSION,
        f"报告 schemaVersion 必须为 {SCHEMA_VERSION}，实际为 {schema if type(schema) in (int, float) else 'missing'}",
    )
    fail(
        suite.get("mode") != mode,
        f"报告模式 {text(suite.get('mode')) or 'unknown'} 与要求的 {mode} 不一致",
    )
    fail(
        suite.get("executionClass") != expected_class,
        f"报告 executionClass 必须为 {expected_class}，实际为 {text(suite.get('executionClass')) or 'missing'}",
    )
    visibility = options["expectedDatasetVisibility"]
    fail(
        dataset.get("schemaVersion") != 1 or dataset.get("visibility") != visibility,
        "报告 dataset visibility 与本次要求不一致",
    )
    fail(
        dataset.get("promptsRedacted") is not (visibility != "public"),
        "报告 dataset promptsRedacted 标记不正确",
    )
    if visibility != "public":
        command = (
            [text(item) for item in metadata.get("command", [])]
            if isinstance(metadata.get("command"), list)
            else []
        )
        for index, argument in enumerate(command):
            fail(
                argument == "--cases-file"
                and (index + 1 >= len(command) or command[index + 1] != "[external-dataset]")
                or argument.startswith("--cases-file=")
                and argument != "--cases-file=[external-dataset]",
                "非公开报告 command 泄漏了外部 cases file 路径",
            )
    fail(
        runtime.get("cli") != "pi",
        "报告 runtime.cli 必须为 pi，实际为 " + (text(runtime.get("cli")) or "missing"),
    )
    fail(
        not all(text(evaluator.get(key)) for key in ("id", "version", "rubricVersion")),
        "报告缺少 evaluator id/version/rubricVersion",
    )
    repeat = selection.get("repeat")
    repeat_valid = safe_integer(repeat) and 1 <= repeat <= 5
    fail(not repeat_valid, "报告 selection.repeat 必须为 1 到 5 的整数")
    if mode == "e2e":
        fail(
            runtime.get("model") != expected_runtime["model"],
            "E2E 报告 runtime.model 必须为 " + expected_runtime["model"],
        )
        fail(
            runtime.get("provider") != expected_runtime["provider"],
            "E2E 报告 runtime.provider 必须为 " + expected_runtime["provider"],
        )
    fail(
        runtime.get("frameworkVersion") != options["frameworkVersion"],
        "报告 frameworkVersion 与当前 PI Agent 不一致",
    )
    fail(
        runtime.get("buildRevision") != options["buildRevision"],
        "报告 buildRevision 与当前构建不一致",
    )
    revision = options["buildRevision"]
    fail(
        mode == "e2e"
        and (
            not revision or revision.startswith("unversioned:") or "dirty.unavailable" in revision
        ),
        "E2E 发布证据要求可重现的 buildRevision，拒绝 unversioned/dirty.unavailable",
    )
    git = text(provenance.get("gitRevision") or provenance.get("gitCommit"))
    fail(
        not options["gitRevision"] or git != options["gitRevision"],
        "报告 git revision 与当前 checkout 不一致",
    )
    for key, expected, message in (
        ("frameworkVersion", "frameworkVersion", "报告 provenance.frameworkVersion 不匹配"),
        ("buildRevision", "buildRevision", "报告 provenance.buildRevision 不匹配"),
        ("casesSha256", "expectedCasesSha256", "报告 casesSha256 与本次要求的 case 集不一致"),
        ("promptsSha256", "expectedPromptsSha256", "报告 promptsSha256 与当前 prompts 不一致"),
        (
            "datasetRegistrySha256",
            "expectedDatasetRegistrySha256",
            "报告 datasetRegistrySha256 与当前数据集合同不一致",
        ),
        (
            "snapshotManifestSha256",
            "expectedSnapshotManifestSha256",
            "报告 snapshotManifestSha256 与当前快照合同不一致",
        ),
    ):
        fail(provenance.get(key) != options[expected], message)
    snapshots = record(metadata.get("dataSnapshots"))
    normalized = [
        {key: text(item.get(key)) for key in ("caseId", "id", "asOf", "payloadSha256")}
        for item in records(snapshots.get("selected"))
    ]
    fail(
        snapshots.get("schemaVersion") != 1
        or sorted(normalized, key=lambda item: item["caseId"])
        != sorted(options["expectedDataSnapshots"], key=lambda item: item["caseId"]),
        "报告 dataSnapshots 与本次 case 对应的可重放快照不一致",
    )
    created, started, finished = (
        parsed(report.get("createdAt")),
        parsed(metadata.get("startedAt")),
        parsed(metadata.get("finishedAt")),
    )
    if not all((created, started, finished)):
        problems.append("报告缺少有效的 createdAt/startedAt/finishedAt 时间证明")
    else:
        fail(
            started > finished or finished > created,
            "报告时间顺序必须满足 startedAt <= finishedAt <= createdAt",
        )
        now = options.get("now") or datetime.now(UTC)
        now = parsed(now) if isinstance(now, str) else now
        fail(created > now + timedelta(minutes=5), "报告 createdAt 位于允许时钟偏差之外的未来")
    ids = [text(item.get("id")) for item in results]
    fail(any(not identity for identity in ids), "报告包含缺少 ID 的 case")
    repeated = duplicates(ids)
    if repeated:
        problems.append("报告包含重复 case：" + ", ".join(repeated))
    if sorted(ids) != sorted(options["expectedCaseIds"]):
        problems.append(
            "报告 case 集不完整；要求 "
            + ", ".join(options["expectedCaseIds"])
            + "，实际 "
            + (", ".join(ids) or "(empty)")
        )
    passed = sum(item.get("passed") is True for item in results)
    failed = len(results) - passed
    fail(report.get("total") != len(results), "报告 total 与 results.length 不一致")
    fail(report.get("passedCount") != passed, "报告 passedCount 与逐 case 结果不一致")
    fail(report.get("failedCount") != failed, "报告 failedCount 与逐 case 结果不一致")
    repeated_evidence = []
    for item in results:
        identity = text(item.get("id")) or "unknown"
        if identity in options["expectedResultQuestions"]:
            fail(
                text(item.get("question")) != options["expectedResultQuestions"][identity],
                identity + " question 证据未按数据集可见性保存",
            )
        evaluation = record(item.get("evaluation"))
        fail(not is_current(evaluation), identity + " 缺少当前版本的 evaluator 结果")
        gate = deterministic_gate({**item, "passed": True, "failures": []})
        fail(
            evaluation.get("hardGatePassed") is True and not gate["passed"],
            identity + " evaluator 成功与底层检查冲突：" + "、".join(gate["failures"]),
        )
        fail(
            any(
                evaluation.get(actual) != evaluator.get(expected)
                for actual, expected in (
                    ("evaluatorId", "id"),
                    ("evaluatorVersion", "version"),
                    ("rubricVersion", "rubricVersion"),
                )
            ),
            identity + " evaluator 身份与报告 metadata 不一致",
        )
        fail(
            type(item.get("firstPassPassed")) is not bool
            or type(item.get("finalPassed")) is not bool,
            identity + " 缺少 firstPassPassed/finalPassed",
        )
        fail(
            item.get("finalPassed") is not item.get("passed")
            or evaluation.get("passed") is not item.get("passed"),
            identity + " final/evaluator/pass 判定不一致",
        )
        fail(
            item.get("score") != evaluation.get("score"),
            identity + " score 与 evaluator score 不一致",
        )
        fail(
            not equal(record(item.get("traceDiagnostics")), trace_diagnostics(item, mode)),
            identity + " traceDiagnostics 与可观察证据重算结果不一致",
        )
        stability = record(item.get("stability"))
        attempts = records(stability.get("attempts"))
        fail(
            stability.get("repeatCount") != repeat or len(attempts) != repeat,
            identity + " stability attempts 与 selection.repeat 不一致",
        )
        passed_attempts = sum(attempt.get("passed") is True for attempt in attempts)
        rate = math.floor(passed_attempts / len(attempts) * 100 + 0.5) if attempts else 0
        fail(
            stability.get("passedAttempts") != passed_attempts
            or stability.get("passRate") != rate
            or stability.get("passed") is not (passed_attempts == len(attempts))
            or stability.get("flaky") is not (0 < passed_attempts < len(attempts)),
            identity + " stability 汇总与逐次结果不一致",
        )
        fail(
            [attempt.get("attempt") for attempt in attempts] != list(range(1, int(repeat) + 1))
            if repeat_valid
            else bool(attempts),
            identity + " stability attempt 序号必须从 1 连续递增",
        )
        for attempt in attempts:
            duration, repairs = (
                number(attempt.get("durationMs"), None),
                number(attempt.get("repairAttempts"), None),
            )
            fail(
                type(attempt.get("passed")) is not bool
                or type(attempt.get("firstPassPassed")) is not bool
                or number(attempt.get("score"), None) is None
                or duration is None
                or repairs is None
                or duration < 0
                or repairs < 0
                or attempt.get("firstPassPassed") is True
                and attempt.get("passed") is not True,
                f"{identity} 第 {attempt.get('attempt')} 次 stability 指标无效",
            )
            if mode == "e2e":
                evidence = record(attempt.get("evidence"))
                fail(
                    not attested(evidence, expected_runtime, framework=options["frameworkVersion"]),
                    f"{identity} 第 {attempt.get('attempt')} 次运行缺少可验真的 E2E 证据",
                )
                fail(
                    attempt.get("agentAttested") is not True,
                    f"{identity} 第 {attempt.get('attempt')} 次运行 agentAttested 必须为 true",
                )
                fail(
                    any(
                        text(attempt.get(key)) != text(evidence.get(key))
                        for key in ("requestId", "projectId", "projectPath")
                    ),
                    f"{identity} 第 {attempt.get('attempt')} 次运行身份摘要与 E2E 证据不一致",
                )
                repeated_evidence.append((identity, attempt.get("attempt"), evidence))
        if attempts:
            primary = attempts[0]
            fail(
                any(
                    primary.get(key) != item.get(key)
                    for key in ("passed", "firstPassPassed", "score")
                ),
                identity + " 顶层结果与首个物理 run 不一致",
            )
    expected_quality = quality_summary(results, int(repeat) if repeat_valid else 1)
    reported_quality = record(report.get("qualitySummary"))
    for key in (
        "caseCount",
        "firstPassCount",
        "finalPassCount",
        "firstPassRate",
        "finalPassRate",
        "repairedCaseCount",
        "repairRate",
        "averageScore",
    ):
        fail(
            reported_quality.get(key) != expected_quality[key],
            "报告 qualitySummary." + key + " 与逐 case 重算不一致",
        )
    for group in ("durationMs", "dimensions"):
        for key, expected in expected_quality[group].items():
            fail(
                record(reported_quality.get(group)).get(key) != expected,
                "报告 qualitySummary."
                + group
                + "."
                + key
                + (" 与逐 case 重算不一致" if group == "durationMs" else " 与逐 case 重算不一致"),
            )
    reported_stability, expected_stability = (
        record(reported_quality.get("stability")),
        expected_quality["stability"],
    )
    for key in ("repeatCount", "attemptCount", "passedAttemptCount", "passRate"):
        fail(
            reported_stability.get(key) != expected_stability[key],
            "报告 qualitySummary.stability." + key + " 与逐次结果不一致",
        )
    fail(
        sorted(reported_stability.get("flakyCaseIds") or [])
        != sorted(expected_stability["flakyCaseIds"]),
        "报告 qualitySummary.stability.flakyCaseIds 与逐次结果不一致",
    )
    for key in ("lower", "upper"):
        fail(
            record(reported_stability.get("confidence95")).get(key)
            != expected_stability["confidence95"][key],
            "报告 qualitySummary.stability.confidence95." + key + " 与逐次结果不一致",
        )
    fail(
        not equal(reported_stability.get("scoreStdDev"), expected_stability["scoreStdDev"]),
        "报告 qualitySummary.stability.scoreStdDev 与逐次分数重算不一致",
    )
    e2e_quality = None
    if mode == "e2e":
        fail(
            retention.get("databaseEvidenceRetained") is not True
            or retention.get("workspaceRetained") is not True,
            "E2E 报告生成时必须保留 AgentRun/Mission 数据库证据和工作空间",
        )
        fail(runtime.get("agentExecuted") is not True, "E2E 报告没有真实 Agent 执行标记")
        execution = execution_summary(
            results, expected_runtime, framework=options["frameworkVersion"]
        )
        if execution["unattestedCaseIds"]:
            problems.append(
                "E2E 报告包含未逐 case 证明 Mission acceptance 的用例："
                + ", ".join(execution["unattestedCaseIds"])
            )
        fail(
            runtime.get("executedCaseCount") != execution["executedCaseCount"],
            "E2E runtime.executedCaseCount 与逐 case 证明不一致",
        )
        fail(
            sorted(runtime.get("unattestedCaseIds") or [])
            != sorted(execution["unattestedCaseIds"]),
            "E2E runtime.unattestedCaseIds 与逐 case 证明不一致",
        )
        for values, prefix in (
            ([(text(item.get("id")) or "unknown", None, item) for item in results], "E2E 跨 case"),
            (repeated_evidence, "E2E 物理重复运行"),
        ):
            for label in (
                "requestId",
                "missionId",
                "generationId",
                "acceptedReceiptId",
                "acceptedReceiptHash",
                "runId",
            ):
                identities = []
                for _, _, item in values:
                    run = record(item.get("agentExecution"))
                    identities.extend(
                        run.get("runIds") or []
                    ) if label == "runId" else identities.append(
                        text(item.get("requestId"))
                        if label == "requestId"
                        else text(run.get(label))
                    )
                reused = duplicates(identities)
                if reused:
                    problems.append(prefix + " 复用了 " + label + "：" + ", ".join(reused))
            for identity, attempt, item in values:
                run = record(item.get("agentExecution"))
                label = identity + (f" 第 {attempt} 次" if attempt is not None else "")
                if attempt is None:
                    for key in ("buildRevision", "gitRevision", "frameworkVersion"):
                        fail(
                            run.get(key) != options[key], identity + " AgentRun " + key + " 不匹配"
                        )
                else:
                    fail(
                        any(
                            run.get(key) != options[key]
                            for key in ("buildRevision", "gitRevision", "frameworkVersion")
                        ),
                        label + " AgentRun 运行版本不匹配",
                    )
                execution_start, execution_end = (
                    parsed(run.get("startedAt")),
                    parsed(run.get("completedAt")),
                )
                fail(
                    bool(started and finished)
                    and (
                        not execution_start
                        or not execution_end
                        or execution_start < started
                        or execution_end > finished
                    ),
                    label + " AgentRun 时间窗口超出报告执行窗口",
                )
        e2e_quality = quality(results, options["qualityThresholds"])
        problems.extend(e2e_quality["problems"])
    else:
        fail(
            runtime.get("agentExecuted") is True
            or any(item.get("agentExecuted") is True for item in results),
            "contract 报告不得声明真实 Agent 执行",
        )
    expected_passed = (
        failed == 0
        and expected_quality["stability"]["passRate"] == 100
        and (e2e_quality["passed"] if e2e_quality else True)
        and not problems
    )
    fail(report.get("passed") is not expected_passed, "报告 passed 与逐 case/质量门计算结果不一致")
    return {
        "passed": not problems,
        "problems": problems,
        "calculated": {"total": len(results), "passedCount": passed, "failedCount": failed},
        "quality": e2e_quality,
    }
