"""Normalize durable benchmark evidence for the existing evaluation workbench."""

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from .config import ROOT
from .evaluation_queue import bounded, strings
from .evaluation_scoring import DIMENSIONS, left_sum, number, quality_summary, result_score
from .strategy_mappers import array, record

CATALOG = json.loads((ROOT / "config/evaluation-catalog.json").read_text())


def text(value, fallback=""):
    return value if isinstance(value, str) else fallback


def boolean(value, fallback=False):
    return value if type(value) is bool else fallback


def records(value):
    return [item for item in array(value) if isinstance(item, dict)]


def fields(value, *, numeric="", nullable="", textual="", boolean_fields="", arrays=""):
    value = record(value)
    return {
        **{key: number(value.get(key)) for key in numeric.split()},
        **{key: text(value.get(key)) or None for key in nullable.split()},
        **{key: text(value.get(key)) for key in textual.split()},
        **{key: boolean(value.get(key)) for key in boolean_fields.split()},
        **{key: strings(value.get(key)) for key in arrays.split()},
    }


def status(value):
    return value if value in ("passed", "failed", "warning") else "unknown"


def checks(value, *, evaluation=False):
    return [
        {
            "id": text(item.get("id"), "unknown"),
            "name": text(
                item.get("name"), text(item.get("id"), "评测项" if evaluation else "检查项")
            ),
            "status": (
                "failed"
                if evaluation and status(item.get("status")) == "unknown"
                else status(item.get("status"))
            ),
            "summary": text(item.get("summary")),
        }
        for item in records(value)
    ]


def skill_snapshot(value):
    value = record(value)
    skills = {}
    for identity, raw in record(value.get("skills")).items():
        item = record(raw)
        skills[identity] = {
            "version": text(item.get("version")) or None,
            "hash": text(item.get("hash")) or text(item.get("sourceSha256")) or None,
            "sourceSha256": text(item.get("sourceSha256")) or text(item.get("hash")) or None,
            "packageHash": text(item.get("packageHash")) or text(item.get("packageSha256")) or None,
            "packageSha256": text(item.get("packageSha256"))
            or text(item.get("packageHash"))
            or None,
            "sourcePath": text(item.get("sourcePath")) or None,
            "packagePath": text(item.get("packagePath")) or None,
        }
    schema = value.get("schemaVersion")
    return {
        "schemaVersion": schema if type(schema) in (str, int, float) else None,
        "skills": skills,
    }


def e2e_quality(value):
    if not isinstance(value, dict):
        return None
    summary = record(value.get("summary"))
    result = {
        "passed": boolean(value.get("passed")),
        "problems": strings(value.get("problems")),
        "thresholds": fields(
            value.get("thresholds"),
            numeric="maxTurnsPerCase maxCacheMissInputTokensPerCase maxUnexpectedToolFailures",
        ),
        "summary": fields(
            summary, numeric="caseCount measuredCaseCount", arrays="missingMetricsCaseIds"
        ),
    }
    for key in ("turns", "cacheMissInputTokens"):
        item = record(summary.get(key))
        result["summary"][key] = {
            **fields(item, numeric="total average"),
            "max": fields(item.get("max"), nullable="id", numeric="value"),
        }
    result["summary"]["tools"] = fields(
        summary.get("tools"), numeric="unexpectedFailureCount", arrays="affectedCaseIds"
    )
    return result


def metadata(report, results):
    value = record(report.get("metadata"))
    queue, runtime, selection, evaluator, suite, dataset, provenance, retention, snapshots = (
        record(value.get(key))
        for key in (
            "queue",
            "runtime",
            "selection",
            "evaluator",
            "suite",
            "dataset",
            "provenance",
            "retention",
            "dataSnapshots",
        )
    )
    concurrency = bounded(
        selection.get("concurrency")
        if selection.get("concurrency") is not None
        else evaluator.get("concurrency"),
        1,
        16,
    )
    schema = report.get("schemaVersion")
    result = {
        "trigger": text(value.get("trigger")) or None,
        "reportSchemaVersion": schema
        if type(schema) in (int, float)
        and math.isfinite(schema)
        and int(schema) == schema
        and abs(schema) <= 9007199254740991
        else None,
        "startedAt": text(value.get("startedAt")) or text(report.get("createdAt")) or None,
        "finishedAt": text(value.get("finishedAt")) or text(report.get("createdAt")) or None,
        "command": strings(value.get("command")),
        "evaluator": {
            **fields(evaluator, nullable="id version rubricVersion"),
            "concurrency": concurrency,
        },
        "runtime": {
            "cli": text(runtime.get("cli")) or "benchmark",
            "model": text(runtime.get("model")) or "deterministic",
            **fields(
                runtime,
                nullable="reasoningEffort configuredModel frameworkVersion buildRevision",
                boolean_fields="agentExecuted",
                numeric="executedCaseCount",
                arrays="unattestedCaseIds",
            ),
        },
        "suite": {
            "mode": "e2e" if suite.get("mode") == "e2e" else "contract",
            "label": text(suite.get("label"))
            or ("DeepSeek 真实生成 E2E" if suite.get("mode") == "e2e" else "确定性产物契约"),
        },
        "dataset": {
            **fields(
                dataset,
                numeric="schemaVersion",
                boolean_fields="promptsRedacted",
                nullable="sourceIdentitySha256",
            ),
            "visibility": dataset.get("visibility")
            if dataset.get("visibility") in ("hidden", "production_replay")
            else "public",
        },
        "retention": fields(retention, boolean_fields="databaseEvidenceRetained workspaceRetained"),
        "provenance": fields(
            provenance,
            nullable="gitCommit gitRevision buildRevision frameworkVersion casesSha256 promptsSha256 datasetRegistrySha256 snapshotManifestSha256",
        ),
        "dataSnapshots": {
            **fields(snapshots, numeric="schemaVersion", nullable="manifestId manifestVersion"),
            "selected": [
                fields(item, textual="caseId id asOf payloadSha256")
                for item in records(snapshots.get("selected"))
                if text(item.get("caseId")) and text(item.get("id"))
            ],
        },
        "e2eQuality": e2e_quality(value.get("e2eQuality")),
        "selection": {
            "selectedCases": strings(selection.get("selectedCases")),
            "limit": selection.get("limit")
            if type(selection.get("limit")) in (int, float)
            else None,
            "keepProjects": boolean(selection.get("keepProjects")),
            "caseCount": number(selection.get("caseCount"), len(results)),
            "concurrency": concurrency,
            "repeat": max(1, number(selection.get("repeat"), 1)),
        },
        "skillLockSnapshot": skill_snapshot(value.get("skillLockSnapshot")),
    }
    if isinstance(queue.get("id"), str) and isinstance(queue.get("leaseToken"), str):
        result["queue"] = {"id": queue["id"], "leaseToken": queue["leaseToken"]}
    if text(suite.get("executionClass")):
        result["suite"]["executionClass"] = suite["executionClass"]
    return result


def coverage(value):
    value = record(value)

    def bucket(raw):
        return {
            key: fields(item, numeric="total passed failed") for key, item in record(raw).items()
        }

    required = record(value.get("requiredCoverage"))
    levels = ("routing", "contract", "live_e2e", "production")
    return {
        **{key: bucket(value.get(key)) for key in ("byCapability", "byType", "byTag")},
        "byLevel": {key: bucket(record(value.get("byLevel")).get(key)) for key in levels},
        "caseLevels": {
            key: [level for level in strings(raw) if level in levels]
            for key, raw in record(value.get("caseLevels")).items()
        },
        **{
            key: {identity: strings(raw) for identity, raw in record(value.get(key)).items()}
            for key in ("caseTags", "failedTags")
        },
        "requiredCoverage": {
            "capabilities": strings(required.get("capabilities")),
            "tags": strings(required.get("tags")),
            "levels": {key: strings(raw) for key, raw in record(required.get("levels")).items()},
        },
    }


def evaluation(value):
    if not isinstance(value, dict):
        return None
    result = {
        **fields(
            value,
            textual="evaluatorId evaluatorVersion rubricVersion",
            boolean_fields="hardGatePassed passed",
            numeric="score",
        ),
        "checks": checks(value.get("checks"), evaluation=True),
        "dimensions": [
            {
                **fields(item, textual="id summary", numeric="score weight"),
                "label": text(item.get("label"), text(item.get("id"))),
                "status": status(item.get("status")),
            }
            for item in records(value.get("dimensions"))
            if item.get("id") in DIMENSIONS
        ],
        "semanticReview": None,
    }
    semantic = value.get("semanticReview")
    if isinstance(semantic, dict):
        result["semanticReview"] = {
            "schemaVersion": 1,
            "reviewer": fields(
                semantic.get("reviewer"),
                textual="provider model promptVersion",
                boolean_fields="independentFromGenerator",
            ),
            "verdict": semantic.get("verdict")
            if semantic.get("verdict") in ("passed", "warning")
            else "failed",
            **fields(semantic, numeric="score", textual="summary"),
            "dimensions": [
                fields(item, textual="id rationale", numeric="score", arrays="evidence")
                for item in records(semantic.get("dimensions"))
            ],
            "usage": fields(semantic.get("usage"), numeric="inputTokens outputTokens totalTokens")
            if isinstance(semantic.get("usage"), dict)
            else None,
        }
    return result


def stability(value):
    if not isinstance(value, dict):
        return None
    attempts = [
        {
            **fields(
                item,
                boolean_fields="passed firstPassPassed",
                numeric="score durationMs repairAttempts",
                nullable="projectId projectPath requestId",
                arrays="failures",
            ),
            "attempt": max(1, number(item.get("attempt"), 1)),
            "agentAttested": item.get("agentAttested")
            if type(item.get("agentAttested")) is bool
            else None,
        }
        for item in records(value.get("attempts"))
    ]
    return {
        "passed": boolean(value.get("passed"), all(item["passed"] for item in attempts)),
        "repeatCount": max(1, number(value.get("repeatCount"), len(attempts) or 1)),
        "passedAttempts": number(
            value.get("passedAttempts"), sum(item["passed"] for item in attempts)
        ),
        **fields(value, numeric="passRate", boolean_fields="flaky"),
        "attempts": attempts,
    }


USAGE_FIELDS = (
    "inputTokens outputTokens totalTokens cachedInputTokens cacheMissInputTokens reasoningTokens"
)
TOOL_FIELDS = "total succeeded failed uncertain unexpectedFailureCount workspaceWriteSucceeded submitResultSucceeded"
RUN_NULLABLE = (
    "requestId status provider model frameworkVersion buildRevision startedAt completedAt"
)
ACCEPTANCE_FIELDS = "missionId generationId status acceptedReceiptId acceptedReceiptHash acceptedReceiptType acceptedReceiptVerdict acceptedSourceRunId acceptedSourceRequestId acceptedCandidateSource"


def agent_run(value):
    return {
        **fields(value, textual="id runInstanceId", nullable=RUN_NULLABLE, numeric="turns"),
        "usage": fields(value.get("usage"), numeric=USAGE_FIELDS),
        "tools": fields(value.get("tools"), numeric=TOOL_FIELDS),
    }


def agent_execution(value):
    if not isinstance(value, dict):
        return None
    nullable = "cli provider model requestId missionId generationId missionStatus acceptedReceiptId acceptedReceiptHash acceptedReceiptType acceptedReceiptVerdict acceptedSourceRunId acceptedSourceRequestId acceptedCandidateSource frameworkVersion buildRevision gitRevision startedAt completedAt"
    return {
        **fields(
            value,
            boolean_fields="executed",
            nullable=nullable,
            numeric="candidateVersion turns",
            arrays="runIds",
        ),
        "runs": [agent_run(item) for item in records(value.get("runs"))],
        "usage": fields(value.get("usage"), numeric=USAGE_FIELDS),
        "tools": fields(value.get("tools"), numeric=TOOL_FIELDS),
    }


def result(raw, cases=None, case_tags=None):
    identity = text(raw.get("id"), "unknown")
    case = (cases or {}).get(identity, {})
    capability, kind = (
        case.get("capabilityId", text(raw.get("capabilityId"), "unknown")),
        case.get("type", text(raw.get("type"), "generated_project")),
    )
    validation, artifacts, prefetch = (
        record(raw.get(key)) for key in ("validation", "artifacts", "prefetch")
    )
    final = record(artifacts.get("finalData"))
    value = {
        "id": identity,
        "name": text(raw.get("name"), case.get("name", identity)),
        "question": text(raw.get("question"), case.get("question", "")),
        **fields(
            raw,
            nullable="projectId projectPath requestId",
            numeric="durationMs repairAttempts platformRepairCount",
            boolean_fields="passed agentExecuted",
            arrays="failures symbols",
        ),
        "firstPassPassed": boolean(
            raw.get("firstPassPassed"),
            boolean(raw.get("passed")) and number(raw.get("repairAttempts")) == 0,
        ),
        "finalPassed": boolean(raw.get("finalPassed"), boolean(raw.get("passed"))),
        "score": result_score(raw),
        "agentExecution": agent_execution(raw.get("agentExecution")),
        "missionAcceptance": fields(
            raw.get("missionAcceptance"), nullable=ACCEPTANCE_FIELDS, numeric="candidateVersion"
        )
        if isinstance(raw.get("missionAcceptance"), dict)
        else None,
        "capabilityId": capability,
        "capabilityLabel": CATALOG["capabilityLabels"].get(capability, capability),
        "type": kind,
        "typeLabel": CATALOG["typeLabels"].get(kind, kind),
        "tags": (case_tags or {}).get(identity, case.get("tags", strings(raw.get("tags")))),
        "validationStatus": status(validation.get("status")),
        "validationChecks": checks(validation.get("checks")),
        "evaluation": evaluation(raw.get("evaluation")),
        "stability": stability(raw.get("stability")),
        "eventAudit": fields(
            raw.get("eventAudit"),
            numeric="total warningCount errorCount",
            arrays="eventTypes stages",
        )
        if isinstance(raw.get("eventAudit"), dict)
        else None,
        "traceDiagnostics": None,
        "artifacts": {
            **fields(
                final,
                nullable="templateId",
                numeric="klineRows reportRows announcementRows tradeRows assetCount holdingCount comparisonRows",
                boolean_fields="hasImageExtraction",
            ),
            "finalDataPath": text(prefetch.get("finalDataPath")) or None,
            "rawFileCount": len(strings(prefetch.get("rawFiles"))),
            "qualityStatus": text(record(artifacts.get("quality")).get("status")) or None,
        },
        "visualCheck": None,
    }
    trace = raw.get("traceDiagnostics")
    if isinstance(trace, dict):
        allowed = ("intent", "planning", "data", "artifact", "visual", "runtime", "acceptance")
        value["traceDiagnostics"] = {
            "schemaVersion": 1,
            "primaryFailureStage": trace.get("primaryFailureStage")
            if trace.get("primaryFailureStage") in allowed
            else None,
            "stages": [
                {
                    **fields(item, textual="id", arrays="signals"),
                    "label": text(item.get("label"), text(item.get("id"))),
                    "status": status(item.get("status")),
                }
                for item in records(trace.get("stages"))
                if item.get("id") in allowed
            ],
            "observedEventStages": strings(trace.get("observedEventStages")),
        }
    visual = raw.get("visualCheck")
    if isinstance(visual, dict):
        value["visualCheck"] = {
            **fields(
                visual,
                boolean_fields="passed",
                nullable="screenshotPath",
                numeric="accessibilityIssueCount",
                arrays="failures",
            ),
            "screenshots": [
                {
                    **fields(item, textual="path", numeric="width height"),
                    "viewport": text(item.get("viewport"), "unknown"),
                }
                for item in records(visual.get("screenshots"))
                if text(item.get("path"))
            ],
        }
    return value


def normalize_run(path, mtime_ms, report, cases, *, root=ROOT):
    path = Path(path)
    covered = coverage(report.get("coverage"))
    results = [
        result(item, {case["id"]: case for case in cases}, covered["caseTags"])
        for item in records(report.get("results"))
    ]
    total = number(report.get("total"), len(results))
    passed = number(report.get("passedCount"), sum(item["passed"] for item in results))
    failed = number(report.get("failedCount"), max(0, total - passed))
    meta = metadata(report, results)
    return {
        "id": path.stem,
        "fileName": path.name,
        "filePath": str(path.relative_to(root)).replace("\\", "/"),
        "createdAt": text(
            report.get("createdAt"),
            datetime.fromtimestamp(mtime_ms / 1000, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        ),
        "mtimeMs": mtime_ms,
        "passed": boolean(report.get("passed"), failed == 0),
        "total": total,
        "passedCount": passed,
        "failedCount": failed,
        "passRate": math.floor(passed / total * 100 + 0.5) if total else 0,
        "averageScore": math.floor(left_sum(item["score"] for item in results) / len(results) + 0.5)
        if results
        else 0,
        "durationMs": left_sum(item["durationMs"] for item in results),
        "metadata": meta,
        "qualitySummary": quality_summary(results, meta["selection"]["repeat"]),
        "e2eQuality": e2e_quality(report.get("e2eQuality")) or meta["e2eQuality"],
        "coverage": covered,
        "results": results,
    }


def analysis(runs, *, skills=False):
    groups = {}
    for run in runs:
        if skills:
            keys = [
                (
                    identity + "@" + (item.get("version") or "unknown"),
                    {"skillId": identity, "version": item.get("version") or "unknown"},
                )
                for identity, item in run["metadata"]["skillLockSnapshot"]["skills"].items()
            ]
        else:
            runtime = run["metadata"]["runtime"]
            entry = {
                "cli": runtime.get("cli") or "unknown",
                "model": runtime.get("model") or "unknown",
                "reasoningEffort": runtime.get("reasoningEffort") or "-",
            }
            key = ":".join(entry.values())
            keys = [(key, {"key": key, **entry})]
        for key, entry in keys:
            group = groups.setdefault(key, {"entry": entry, "runs": []})
            group["runs"].append(run)
    values = []
    for group in groups.values():
        ordered = sorted(group["runs"], key=lambda run: -run["mtimeMs"])
        latest = ordered[0]
        values.append(
            {
                **group["entry"],
                "runs": len(ordered),
                "latestRunId": latest["id"],
                "latestPassRate": latest["passRate"],
                "averagePassRate": math.floor(
                    left_sum(run["passRate"] for run in ordered) / len(ordered) + 0.5
                ),
                "latestAverageScore": latest["averageScore"],
                "averageScore": math.floor(
                    left_sum(run["averageScore"] for run in ordered) / len(ordered) + 0.5
                ),
                "latestCreatedAt": latest["createdAt"],
            }
        )
    ordered = sorted(values, key=lambda item: item["latestCreatedAt"], reverse=True)
    return ordered[:30] if skills else ordered
