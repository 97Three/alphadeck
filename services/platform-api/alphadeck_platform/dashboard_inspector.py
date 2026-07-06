import asyncio
import json
from copy import deepcopy

from .config import ROOT
from .file_tools import record
from .inspector_reports import (
    render_report,
    summary_final,
    summary_plan,
    summary_quality,
    summary_sources,
)
from .inspector_source import (
    outline,
    page_signals,
    proxy_signals,
    responsive_signals,
    visual_source_map,
)
from .skill_integrity import read_regular
from .tool_paths import ToolError

CATALOG = json.loads((ROOT / "config/dashboard-inspector-catalog.json").read_text())


def public_file(file):
    return {key: value for key, value in file.items() if key != "content"}


class DashboardInspector:
    def __init__(self, policy, *, timeout_ms=15000, max_output_chars=12000, max_file_bytes=2000000):
        self.policy, self.timeout, self.output, self.maximum = (
            policy,
            timeout_ms,
            max_output_chars,
            max_file_bytes,
        )
        self.definition = deepcopy(CATALOG["definition"])

    def inspect_file(self, candidates):
        for candidate in candidates:
            try:
                resolved = self.policy.read(candidate)
            except ToolError as cause:
                if cause.code == "PATH_NOT_FOUND":
                    continue
                raise
            path, relative = resolved.canonical, resolved.relative
            stat = path.stat()
            if not path.is_file():
                return {
                    "path": relative,
                    "exists": True,
                    "bytes": stat.st_size,
                    "analysisSkipped": "not_a_file",
                }
            if stat.st_size > self.maximum:
                return {
                    "path": relative,
                    "exists": True,
                    "bytes": stat.st_size,
                    "analysisSkipped": "file_too_large",
                }
            data = read_regular(path, self.maximum)
            if b"\x00" in data:
                return {
                    "path": relative,
                    "exists": True,
                    "bytes": len(data),
                    "analysisSkipped": "binary_file",
                }
            content = data.decode("utf-8", "replace")
            lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
            return {
                "path": relative,
                "exists": True,
                "bytes": len(data),
                "totalLines": lines,
                "content": content,
            }
        return {"path": candidates[0], "exists": False}

    def inspect(self):
        candidates = deepcopy(CATALOG["candidates"])
        if (self.policy.root / "vite.config.ts").is_file():
            candidates["marketProxy"] = ["vite.config.ts"]
        files = {key: self.inspect_file(paths) for key, paths in candidates.items()}
        missing = [file["path"] for file in files.values() if not file["exists"]]
        source_outline = outline(files["page"])
        report = {
            "schemaVersion": 1,
            "advisory": CATALOG["advisory"],
            "files": {key: public_file(file) for key, file in files.items()},
            "missingRequiredFiles": missing,
            "runPlan": summary_plan(files["runPlan"]),
            "artifacts": {
                "finalData": summary_final(files["finalData"]),
                "sources": summary_sources(files["sources"]),
                "dataQuality": summary_quality(files["dataQuality"]),
            },
            "pageContract": page_signals(files["page"]),
            "marketProxy": proxy_signals(files["marketProxy"]),
            "responsive": responsive_signals(files["styles"]),
            "outline": source_outline,
            "uiInspection": visual_source_map(
                files["page"], files["styles"], source_outline, CATALOG["preparedVisualEdit"]
            ),
            "nextActions": CATALOG["nextActions"],
        }
        rendered = render_report(report, self.output, CATALOG["nextAction"])
        return {
            "ok": True,
            "data": {
                "schemaVersion": 1,
                "inspectedFiles": sum(file["exists"] for file in files.values()),
                "missingRequiredFiles": missing,
                "contentTruncated": rendered["truncated"],
                "originalContentChars": rendered["originalChars"],
            },
            "content": rendered["content"],
        }

    async def execute(self, value, context=None):
        try:
            if record(value):
                raise ToolError(
                    "INVALID_TOOL_INPUT",
                    "inspect_dashboard_contract does not accept paths or other arguments.",
                )
            async with asyncio.timeout(self.timeout / 1000):
                return self.inspect()
        except TimeoutError:
            error = ToolError("TOOL_TIMEOUT", f"PI Agent tool timed out after {self.timeout}ms.")
        except ToolError as cause:
            error = cause
        except Exception as cause:
            error = ToolError("TOOL_EXECUTION_FAILED", str(cause))
        return {
            "ok": False,
            "error": {
                "code": error.code,
                "message": error.message,
                **({"details": error.details} if error.details is not None else {}),
            },
            "content": error.message,
        }
