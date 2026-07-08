import math
import re

from .evaluation_queue import date
from .evaluation_reports import CATALOG, TOOL_FIELDS, USAGE_FIELDS
from .evaluation_scoring import record

DEFAULT_THRESHOLDS = {
    "maxTurnsPerCase": 12,
    "maxCacheMissInputTokensPerCase": 84000,
    "maxUnexpectedToolFailures": 0,
}
TERMINAL = ("candidate_complete", "completed", "failed", "cancelled", "timed_out", "interrupted")


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def integer(value, *, positive=False):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and value == int(value)
        and (1 if positive else 0) <= value <= 9007199254740991
    )


def window(started, completed):
    try:
        start, end = (
            date(started) if isinstance(started, str) else None,
            date(completed) if isinstance(completed, str) else None,
        )
        return bool(start and end and end >= start)
    except (ValueError, TypeError):
        return False


def usage_present(value):
    value = record(value)
    return (
        all(
            integer(value.get(key), positive=key in ("inputTokens", "totalTokens"))
            for key in USAGE_FIELDS.split()
        )
        and value["cachedInputTokens"] + value["cacheMissInputTokens"] == value["inputTokens"]
        and value["inputTokens"] + value["outputTokens"] == value["totalTokens"]
    )


def tools_present(value):
    value = record(value)
    return (
        all(integer(value.get(key)) for key in TOOL_FIELDS.split())
        and value["succeeded"] + value["failed"] + value["uncertain"] == value["total"]
        and value["unexpectedFailureCount"] == value["failed"] + value["uncertain"]
    )


def lineage(root, request):
    return nonempty(request) and (
        request == root or request.startswith(root + "-validation-repair")
    )


def metrics_present(value):
    execution = record(value.get("agentExecution"))
    runs = execution.get("runs")
    if (
        not nonempty(execution.get("requestId"))
        or not isinstance(runs, list)
        or not runs
        or not integer(execution.get("turns"), positive=True)
        or not usage_present(execution.get("usage"))
        or not tools_present(execution.get("tools"))
    ):
        return False
    for raw in runs:
        run = record(raw)
        if (
            not nonempty(run.get("id"))
            or not nonempty(run.get("runInstanceId"))
            or not lineage(execution["requestId"], run.get("requestId"))
            or run.get("status") not in TERMINAL
            or any(
                run.get(key) != execution.get(key)
                for key in ("provider", "model", "frameworkVersion", "buildRevision")
            )
            or not window(run.get("startedAt"), run.get("completedAt"))
            or not integer(run.get("turns"), positive=True)
            or not usage_present(run.get("usage"))
            or not tools_present(run.get("tools"))
        ):
            return False
    ids = [run["id"] for run in runs]
    if len(set(ids)) != len(ids) or execution.get("runIds") != ids:
        return False
    if execution["turns"] != sum(run["turns"] for run in runs):
        return False
    for group, fields in (("usage", USAGE_FIELDS), ("tools", TOOL_FIELDS)):
        if any(
            execution[group].get(key) != sum(run[group].get(key, 0) for run in runs)
            for key in fields.split()
        ):
            return False
    accepted = next(
        (run for run in runs if run["id"] == execution.get("acceptedSourceRunId")), None
    )
    return bool(
        accepted
        and accepted["status"] == "candidate_complete"
        and accepted.get("requestId") == execution.get("acceptedSourceRequestId")
        and integer(accepted["tools"].get("workspaceWriteSucceeded"), positive=True)
        and integer(accepted["tools"].get("submitResultSucceeded"), positive=True)
    )


def acceptance_matches(value):
    execution, acceptance = (
        record(value.get("agentExecution")),
        record(value.get("missionAcceptance")),
    )
    fields = [
        "missionId",
        "generationId",
        "candidateVersion",
        "acceptedReceiptId",
        "acceptedReceiptHash",
        "acceptedReceiptType",
        "acceptedReceiptVerdict",
        "acceptedSourceRunId",
        "acceptedSourceRequestId",
        "acceptedCandidateSource",
    ]
    return bool(
        execution
        and acceptance
        and all(acceptance.get(key) == execution.get(key) for key in fields)
        and acceptance.get("status") == execution.get("missionStatus")
    )


def attested(value, expected=None, *, framework="python-1"):
    expected = expected or {"provider": "openai", "model": CATALOG["defaultModel"]}
    execution, validation = record(value.get("agentExecution")), record(value.get("validation"))
    checks = validation.get("checks")
    evidence = (
        value.get("passed") is True
        and isinstance(value.get("failures"), list)
        and not value["failures"]
        and validation.get("status") == "passed"
        and isinstance(checks, list)
        and bool(checks)
        and all(record(check).get("status") != "failed" for check in checks)
        and (value.get("visualCheck") is None or record(value["visualCheck"]).get("passed") is True)
        and (value.get("eventAudit") is None or record(value["eventAudit"]).get("errorCount") == 0)
        and acceptance_matches(value)
    )
    return bool(
        value.get("agentExecuted") is True
        and execution.get("executed") is True
        and execution.get("cli") == "pi"
        and execution.get("provider") == expected["provider"]
        and execution.get("model") == expected["model"]
        and nonempty(execution.get("requestId"))
        and value.get("requestId") == execution["requestId"]
        and isinstance(execution.get("runIds"), list)
        and bool(execution["runIds"])
        and all(nonempty(identity) for identity in execution["runIds"])
        and all(
            nonempty(execution.get(key))
            for key in (
                "missionId",
                "generationId",
                "acceptedReceiptId",
                "acceptedSourceRunId",
                "acceptedSourceRequestId",
                "buildRevision",
                "gitRevision",
            )
        )
        and execution.get("missionStatus") == "completed"
        and integer(execution.get("candidateVersion"), positive=True)
        and isinstance(execution.get("acceptedReceiptHash"), str)
        and re.fullmatch(r"sha256:[a-f0-9]{64}", execution["acceptedReceiptHash"])
        and execution.get("acceptedReceiptType") == "acceptance"
        and execution.get("acceptedReceiptVerdict") == "accepted"
        and execution.get("acceptedCandidateSource") == "pi_agent_submit_result"
        and execution.get("frameworkVersion") == framework
        and window(execution.get("startedAt"), execution.get("completedAt"))
        and metrics_present(value)
        and evidence
    )


def execution_summary(results, expected=None, *, framework="python-1"):
    unattested = [
        value.get("id") or "unknown"
        for value in results
        if not attested(value, expected, framework=framework)
    ]
    return {
        "agentExecuted": bool(results) and not unattested,
        "executedCaseCount": len(results) - len(unattested),
        "unattestedCaseIds": unattested,
    }


def quality(results, thresholds=None):
    thresholds = thresholds or DEFAULT_THRESHOLDS
    measured = [value for value in results if metrics_present(value)]
    missing = [value.get("id") or "unknown" for value in results if not metrics_present(value)]

    def aggregate(select):
        values = [
            {"id": value.get("id") or "unknown", "value": select(value["agentExecution"])}
            for value in measured
        ]
        maximum = {"id": None, "value": 0}
        for value in values:
            if value["value"] > maximum["value"]:
                maximum = value
        total = sum(value["value"] for value in values)
        return {
            "total": total,
            "average": int(total / len(values) + 0.5) if values else 0,
            "max": maximum,
        }

    unexpected = sum(
        value["agentExecution"]["tools"]["unexpectedFailureCount"] for value in measured
    )
    summary = {
        "caseCount": len(results),
        "measuredCaseCount": len(measured),
        "missingMetricsCaseIds": missing,
        "turns": aggregate(lambda value: value["turns"]),
        "cacheMissInputTokens": aggregate(lambda value: value["usage"]["cacheMissInputTokens"]),
        "tools": {
            "unexpectedFailureCount": unexpected,
            "affectedCaseIds": [
                value.get("id") or "unknown"
                for value in measured
                if value["agentExecution"]["tools"]["unexpectedFailureCount"] > 0
            ],
        },
    }
    problems = ["E2E 用例缺少真实运行指标：" + ", ".join(missing)] if missing else []
    for value in measured:
        identity, execution = value.get("id") or "unknown", value["agentExecution"]
        if execution["turns"] > thresholds["maxTurnsPerCase"]:
            problems.append(
                f"{identity} turns={execution['turns']} 超过阈值 {thresholds['maxTurnsPerCase']}"
            )
        if (
            execution["usage"]["cacheMissInputTokens"]
            > thresholds["maxCacheMissInputTokensPerCase"]
        ):
            problems.append(
                f"{identity} cache-miss input tokens={execution['usage']['cacheMissInputTokens']} 超过阈值 {thresholds['maxCacheMissInputTokensPerCase']}"
            )
    if unexpected > thresholds["maxUnexpectedToolFailures"]:
        problems.append(
            f"unexpected tool failures={unexpected} 超过阈值 {thresholds['maxUnexpectedToolFailures']}"
        )
    return {
        "passed": not problems,
        "problems": problems,
        "summary": summary,
        "thresholds": thresholds,
    }
