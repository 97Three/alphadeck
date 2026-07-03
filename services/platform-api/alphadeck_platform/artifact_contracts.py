import json
import re
from functools import lru_cache
from itertools import islice

from jsonschema import Draft202012Validator

from .baseline_evidence import present
from .config import ROOT
from .finance_values import coalesce, record
from .generation_state import LABELS
from .prefetch_market import timestamp
from .provenance import js_json
from .research_time import ResearchTimeError, resolve_research_time
from .validation_inputs import read_json, read_text, records

REPORT = ".data-agent/artifact-contracts.json"
EXPECTED = [
    ".data-agent/workspace.json",
    ".data-agent/profile.json",
    ".data-agent/task.json",
    ".data-agent/plan.json",
    ".data-agent/finance-query-rewrite.json",
    ".data-agent/finance-run-plan.json",
    ".data-agent/generation-state.json",
    ".data-agent/generation-queue.json",
    ".data-agent/events.jsonl",
    REPORT,
    ".data-agent/visual-validation.json",
    ".data-agent/validation.json",
    "evidence/sources.json",
    "evidence/data_quality.json",
    "data_file/final/dashboard-data.json",
    "src/App.vue",
]


@lru_cache(maxsize=1)
def definitions():
    value = json.loads((ROOT / "config/artifact-contracts-catalog.json").read_text())
    if value.get("schemaVersion") != 1:
        raise ValueError("Unsupported artifact schemas")
    return [(item, Draft202012Validator(item["schema"])) for item in value["contracts"]]


def has_value(value, keys):
    return isinstance(value, dict) and any(present(value.get(key)) for key in keys)


def has_array(value, keys):
    return isinstance(value, dict) and any(
        isinstance(value.get(key), list) and value[key] for key in keys
    )


def empty_result(value):
    screener = record(value.get("screener")) or {}
    total = screener.get("total_candidates", "undefined")
    zero = (
        total is None
        or total is False
        or type(total) in {int, float}
        and total == 0
        or isinstance(total, str)
        and total.strip() in {"", "0"}
    )
    return (
        value.get("status") == "no_candidates"
        and value.get("assets") == []
        and screener.get("candidates") == []
        and zero
        and has_value(screener, ["source"])
        and has_value(screener, ["fetched_at", "as_of", "trade_date"])
        and all(
            isinstance((record(value.get(key)) or {}).get("rows"), list)
            for key in ("comparison", "selectionRanking", "financialQuality")
        )
        and isinstance(value.get("warnings"), list)
        and bool(value["warnings"])
    )


def extra_errors(kind, value):
    value = record(value) or {}
    if kind == "inspectRunPlan":
        expected = (
            value.get("expectedArtifacts")
            if isinstance(value.get("expectedArtifacts"), list)
            else []
        )
        return [
            "expectedArtifacts 缺少 " + path + "。" for path in EXPECTED if path not in expected
        ]
    if kind == "inspectGenerationState":
        present_steps = {
            step.get("id")
            for step in records(value.get("steps"))
            if isinstance(step.get("id"), str)
        }
        return [
            "steps 缺少 " + step + "。"
            for step in LABELS
            if step not in {"preview", "evidence_verification"} and step not in present_steps
        ]
    if kind == "inspectSources":
        result = []
        for index, source in enumerate(value.get("sources", [])):
            result.extend(
                f"sources[{index}].{key} 缺失。"
                for key in ("source", "endpoint", "artifact_path")
                if not has_value(source, [key])
            )
            if not has_value(source, ["fetched_at", "as_of", "quote_time"]):
                result.append(f"sources[{index}] 缺少 fetched_at/as_of/quote_time。")
        return result
    if kind == "inspectDataQuality":
        result = []
        if not has_array(value, ["datasets", "checks"]):
            result.append("data_quality.json 需要包含 datasets 或 checks。")
        if not has_array(value, ["warnings", "limitations"]) and not re.search(
            "row_count|missing_fields|fetched_at", js_json(value), re.I
        ):
            result.append("data_quality.json 需要记录样本数、缺失字段、警告或限制。")
        return result
    if kind == "inspectDashboardData":
        if not value:
            return ["dashboard-data.json 必须是非空对象。"]
        assets = records(value.get("assets")) or [value]
        market = any(
            has_value(asset, ["symbol", "name"])
            and (
                has_value(record(asset.get("quote")), ["price", "latest", "latest_price", "close"])
                or has_array(
                    coalesce(record(asset.get("kline")), record(asset.get("history"))),
                    ["bars", "data", "items"],
                )
                or has_array(asset, ["bars", "history", "klines", "candles"])
            )
            for asset in assets
        )
        errors = (
            []
            if market or empty_result(value)
            else [
                "dashboard-data.json 至少需要包含可用行情样本，或包含可追溯的 no_candidates 空筛选结果。"
            ]
        )
        if isinstance(value.get("visualization"), dict) and not has_value(
            value["visualization"], ["template_id", "templateId"]
        ):
            errors.append("visualization 存在时必须声明 template_id/templateId。")
        return errors
    return []


def check_contract(root, definition, validator):
    relative, label = definition["relativePath"], definition["label"]
    result = {
        "id": definition["id"],
        "label": label,
        "path": relative,
        "required": definition["required"],
    }
    raw = read_text(root, relative)
    if not raw:
        return {
            **result,
            "status": "failed" if definition["required"] else "passed",
            "summary": "缺少或为空：" + relative
            if definition["required"]
            else label + "尚未生成（可选产物）。",
        }
    value = read_json(root, relative)
    if value is None and raw.strip() != "null":
        return {
            **result,
            "status": "failed" if definition["required"] else "passed",
            "summary": "JSON 解析失败：" + relative
            if definition["required"]
            else label + "尚未生成（可选产物）。",
        }
    issues = list(islice(validator.iter_errors(value), 12))
    if issues:
        return {
            **result,
            "status": "failed",
            "summary": label + "结构不符合契约。",
            "details": "\n".join(
                (".".join(map(str, issue.absolute_path)) or "<root>") + ": " + issue.message
                for issue in issues
            ),
        }
    if definition["id"] == "finance_query_rewrite_contract" and value.get("timeRange"):
        try:
            resolve_research_time(value["timeRange"])
        except ResearchTimeError:
            return {
                **result,
                "status": "failed",
                "summary": label + "结构不符合契约。",
                "details": "timeRange: Historical research requires valid structured dates and a non-future cutoff.",
            }
    extra = extra_errors(definition.get("extraValidate"), value)
    if extra:
        return {
            **result,
            "status": "failed" if definition["required"] else "warning",
            "summary": label + "缺少关键业务字段。",
            "details": "\n".join(extra[:16]),
        }
    return {**result, "status": "passed", "summary": label + "通过。"}


async def validate_contracts(project, artifacts, request=None):
    now = timestamp()
    checks = [check_contract(artifacts.root, item, validator) for item, validator in definitions()]
    failures = [item for item in checks if item["required"] and item["status"] == "failed"]
    warnings = [item for item in checks if item["status"] == "warning"]
    status = "failed" if failures else "warning" if warnings else "passed"
    report = {
        "schemaVersion": 1,
        "projectId": project,
        "requestId": request,
        "status": status,
        "passed": status != "failed",
        "reportPath": REPORT,
        "checks": checks,
        "createdAt": now,
        "updatedAt": now,
    }
    await artifacts.write(REPORT, report)
    await artifacts.event(
        {
            "event_type": "artifact_contracts_checked",
            "stage": "validation",
            "status": "error"
            if status == "failed"
            else "warning"
            if status == "warning"
            else "success",
            **({"run_id": request} if request is not None else {}),
            "artifact_path": REPORT,
            "summary": f"产物契约未通过：{len(failures)} 个必需契约失败。"
            if status == "failed"
            else f"产物契约通过但有 {len(warnings)} 个警告。"
            if status == "warning"
            else "产物契约全部通过。",
            "created_at": now,
        }
    )
    return report


async def check_artifact_contracts(project, artifacts, request=None):
    report = await validate_contracts(project, artifacts, request)
    failed = [item for item in report["checks"] if item["status"] == "failed"]
    warnings = [item for item in report["checks"] if item["status"] == "warning"]
    if failed:
        return {
            "status": "failed",
            "summary": f"产物契约未通过：{len(failed)} 个结构性问题。",
            "details": "\n\n".join(
                item["label"]
                + "："
                + item["summary"]
                + ("\n" + item["details"] if item.get("details") else "")
                for item in failed
            ),
            "metadata": {"reportPath": REPORT, "failed": [item["id"] for item in failed]},
        }
    return {
        "status": "warning" if warnings else "passed",
        "summary": f"产物契约通过但有 {len(warnings)} 个警告。"
        if warnings
        else "关键 JSON 产物契约通过。",
        "metadata": {"reportPath": REPORT, "warningCount": len(warnings)},
    }
