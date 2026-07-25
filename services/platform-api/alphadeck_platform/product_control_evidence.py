"""Recompute zero-model product controls separately from live model evidence."""

import re

from .evaluation_attestation import integer, nonempty, window
from .evaluation_reports import USAGE_FIELDS
from .evaluation_scoring import record

PROVIDER = "pi-agent-trusted-renderer"
MODEL = "pi-agent-deterministic-renderer-v1"


def attest_product_controls(evidence, suite, build):
    evidence = record(evidence)
    problems = []
    results = evidence.get("results") if isinstance(evidence.get("results"), list) else []
    ids = [record(result).get("id") for result in results]
    expected = suite["productControlCaseIds"]
    checks = (
        (
            type(evidence.get("schemaVersion")) is int and evidence.get("schemaVersion") == 1,
            "product controls schemaVersion 必须为 1。",
        ),
        (evidence.get("suiteId") == suite["id"], "product controls suiteId 不匹配。"),
        (
            evidence.get("suiteSchemaVersion") == suite["schemaVersion"],
            "product controls suiteSchemaVersion 不匹配。",
        ),
        (
            all(
                evidence.get(key) == build[key]
                for key in ("frameworkVersion", "buildRevision", "gitRevision")
            )
            and bool(build["gitRevision"]),
            "product controls 运行版本不匹配。",
        ),
        (
            all(isinstance(value, str) for value in ids) and sorted(ids) == sorted(expected),
            "product controls case 集不完整。",
        ),
        (evidence.get("caseIds") == expected, "product controls caseIds 与 suite 不匹配。"),
        (
            window(evidence.get("startedAt"), evidence.get("finishedAt")),
            "product controls 缺少有效执行时间窗口。",
        ),
    )
    problems.extend(message for passed, message in checks if not passed)
    for label, values in (
        ("requestId", [record(result).get("requestId") for result in results]),
        *[
            (key, [record(record(result).get("agentExecution")).get(key) for result in results])
            for key in ("missionId", "generationId", "acceptedReceiptId")
        ],
        (
            "runId",
            [
                identity
                for result in results
                for identity in (
                    record(record(result).get("agentExecution")).get("runIds")
                    if isinstance(record(record(result).get("agentExecution")).get("runIds"), list)
                    else []
                )
            ],
        ),
    ):
        normalized = [value for value in values if nonempty(value)]
        if len(normalized) != len(values) or len(set(normalized)) != len(normalized):
            problems.append(f"product controls {label} 必须完整且跨 case 唯一。")
    for raw in results:
        result = record(raw)
        prefix = str(result.get("id") or "unknown")
        execution = record(result.get("agentExecution"))
        acceptance = record(result.get("missionAcceptance"))
        runs = execution.get("runs") if isinstance(execution.get("runs"), list) else []
        accepted = next(
            (
                record(run)
                for run in runs
                if record(run).get("id") == execution.get("acceptedSourceRunId")
            ),
            {},
        )
        if result.get("passed") is not True or result.get("failures") != []:
            problems.append(prefix + " product control 未通过。")
        if not (
            result.get("agentExecuted") is True
            and execution.get("executed") is True
            and execution.get("cli") == "pi"
            and execution.get("provider") == PROVIDER
            and execution.get("model") == MODEL
            and result.get("requestId") == execution.get("requestId")
            and execution.get("turns") == 2
            and window(execution.get("startedAt"), execution.get("completedAt"))
        ):
            problems.append(prefix + " 没有证明 deterministic_standard PI Agent 执行。")
        if any(
            execution.get(key) != build[key]
            for key in ("frameworkVersion", "buildRevision", "gitRevision")
        ):
            problems.append(prefix + " product control 运行版本不匹配。")
        receipt_keys = (
            "missionId",
            "generationId",
            "acceptedReceiptId",
            "acceptedReceiptHash",
            "acceptedSourceRunId",
            "acceptedSourceRequestId",
            "acceptedCandidateSource",
        )
        if not (
            execution.get("missionStatus") == "completed"
            and acceptance.get("status") == "completed"
            and execution.get("acceptedReceiptType") == "acceptance"
            and execution.get("acceptedReceiptVerdict") == "accepted"
            and execution.get("acceptedCandidateSource") == "pi_agent_submit_result"
            and all(acceptance.get(key) == execution.get(key) for key in receipt_keys)
            and re.fullmatch(
                r"sha256:[a-f0-9]{64}", str(execution.get("acceptedReceiptHash") or "")
            )
        ):
            problems.append(prefix + " 缺少当前 Mission accepted receipt。")
        if not (
            len(runs) == 1
            and execution.get("runIds") == [accepted.get("id")]
            and nonempty(accepted.get("id"))
            and accepted.get("status") == "candidate_complete"
            and accepted.get("provider") == PROVIDER
            and accepted.get("model") == MODEL
            and accepted.get("requestId") == execution.get("acceptedSourceRequestId")
            and all(
                accepted.get(key) == build[key] for key in ("frameworkVersion", "buildRevision")
            )
            and accepted.get("turns") == 2
            and window(accepted.get("startedAt"), accepted.get("completedAt"))
        ):
            problems.append(prefix + " deterministic candidate lineage 无效。")
        if not all(
            integer(record(container.get("usage")).get(key))
            and record(container.get("usage"))[key] == 0
            for container in (execution, accepted)
            for key in USAGE_FIELDS.split()
        ):
            problems.append(prefix + " deterministic standard 必须提供零模型 Token 证明。")
        tools = record(execution.get("tools"))
        expected_tools = {
            "total": 2,
            "succeeded": 2,
            "workspaceWriteSucceeded": 1,
            "submitResultSucceeded": 1,
            "unexpectedFailureCount": 0,
            "failed": 0,
            "uncertain": 0,
        }
        names = tools.get("succeededToolNames")
        if not (
            all(
                type(tools.get(key)) is int and tools[key] == value
                for key, value in expected_tools.items()
            )
            and isinstance(names, list)
            and all(isinstance(value, str) for value in names)
            and sorted(names) == ["apply_dashboard_spec", "submit_result"]
        ):
            problems.append(prefix + " 缺少成功 workspace write/submit_result 或存在工具失败。")
        validation, visual, audit = (
            record(result.get("validation")),
            result.get("visualCheck"),
            result.get("eventAudit"),
        )
        if not (
            validation.get("status") == "passed"
            and isinstance(validation.get("checks"), list)
            and not any(record(check).get("status") == "failed" for check in validation["checks"])
            and (visual is None or record(visual).get("passed") is True)
            and (audit is None or record(audit).get("errorCount") == 0)
        ):
            problems.append(prefix + " 产品验证或视觉/事件验收未通过。")
    if evidence.get("passed") is not (not problems):
        problems.append("product controls passed 与逐项重算结果不一致。")
    return {"passed": not problems, "problems": problems}
