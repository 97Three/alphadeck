import json
from datetime import UTC, datetime

from .scaffold import controlled_path, read, record

STALE_PATHS = (
    ".data-agent/finance-run-plan.json",
    "src/App.vue",
    "src/style.css",
    "src/main.ts",
    "vite.config.ts",
    "data_file/final/dashboard-data.json",
    "evidence/sources.json",
    "evidence/data_quality.json",
    "evidence/image_extraction.json",
    "package.json",
)


def assess_freshness(reportRunId, currentRunId, reportMtimeMs, artifacts):
    report, current = (reportRunId or "").strip() or None, (currentRunId or "").strip() or None
    stale = [item for item in artifacts if item["mtimeMs"] > reportMtimeMs]
    reasons = (["artifact_modified_after_report"] if stale else []) + (
        ["run_id_mismatch"] if current and report != current else []
    )
    return {
        "stale": bool(reasons),
        "reasons": reasons,
        "staleArtifactPaths": [item["path"] for item in stale],
        "newestArtifactMtimeMs": max(item["mtimeMs"] for item in stale) if stale else None,
        "reportRunId": report,
        "currentRunId": current,
    }


def current_run(root):
    value = record(root, ".data-agent/generation-state.json").get("requestId")
    if not isinstance(value, str) or not value.strip():
        value = record(root, ".data-agent/finance-run-plan.json").get("runId")
    return value.strip() if isinstance(value, str) and value.strip() else None


def millis_iso(value):
    return (
        datetime.fromtimestamp(value / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def read_report(root):
    try:
        raw = read(root, ".data-agent/validation.json")
        if not raw:
            return None
        report = json.loads(raw)
        if not isinstance(report, dict):
            return None
        report_mtime = (
            controlled_path(root, ".data-agent/validation.json").stat().st_mtime_ns / 1000000
        )
        artifacts = []
        for relative in STALE_PATHS:
            path = controlled_path(root, relative)
            if path.is_file():
                artifacts.append({"path": relative, "mtimeMs": path.stat().st_mtime_ns / 1000000})
        freshness = assess_freshness(
            report.get("runId"), current_run(root), report_mtime, artifacts
        )
        if freshness["stale"]:
            changed_run = "run_id_mismatch" in freshness["reasons"]
            changed_files = "artifact_modified_after_report" in freshness["reasons"]
            summary = (
                "当前生成轮次和关键产物均已变化，需要重新运行自动验证。"
                if changed_run and changed_files
                else "验证报告不属于当前生成轮次，需要重新运行自动验证。"
                if changed_run
                else "生成产物在上次验证后发生变化，需要重新运行自动验证。"
            )
            report = {
                **report,
                "checks": [
                    *(report.get("checks") if isinstance(report.get("checks"), list) else []),
                    {
                        "id": "validation_report_stale",
                        "name": "验证报告已过期",
                        "status": "warning",
                        "summary": summary,
                        "metadata": {
                            "reasons": freshness["reasons"],
                            "reportUpdatedAt": millis_iso(report_mtime),
                            "staleArtifactPaths": freshness["staleArtifactPaths"],
                            "reportRunId": freshness["reportRunId"],
                            "currentRunId": freshness["currentRunId"],
                            **(
                                {
                                    "newestArtifactUpdatedAt": millis_iso(
                                        freshness["newestArtifactMtimeMs"]
                                    )
                                }
                                if freshness["newestArtifactMtimeMs"] is not None
                                else {}
                            ),
                        },
                    },
                ],
            }
        return report
    except (OSError, ValueError, TypeError):
        return None
