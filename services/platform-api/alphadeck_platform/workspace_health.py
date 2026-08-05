import asyncio
import json
import math
import os
from datetime import UTC, datetime

from .config import ROOT
from .generation_state import GenerationState, now
from .projects import settings_record
from .queue_projection import queue_projection
from .scaffold import controlled_path
from .service_catalog import degradation_config
from .sessions import iso
from .skill_integrity import read_regular

LABELS = {
    "idle": "空闲",
    "awaiting_input": "等待补充",
    "queued": "排队中",
    "running": "生成中",
    "repairing": "修复中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}
ACTIVE = {"awaiting_input", "queued", "running", "repairing"}
RANK = {"healthy": 0, "warning": 1, "unknown": 2, "failed": 3}


def derive_lifecycle(*, generationState=None, queue=None, runPlanStatus=None):
    state, queue = generationState or {}, queue or {}
    items = queue.get("items") or []
    running = next((item for item in items if item.get("status") == "running"), None)
    queued = next((item for item in items if item.get("status") == "queued"), None)
    request = state.get("requestId", queue.get("activeRequestId"))
    status = "idle"
    if state.get("status") in ("cancelled", "failed", "completed"):
        status = state["status"]
    elif (
        state.get("status") == "needs_clarification"
        or generationState is None
        and runPlanStatus == "needs_clarification"
    ):
        status = "awaiting_input"
    elif state.get("status") == "repairing":
        status = "repairing"
    elif running or state.get("status") in ("running", "pending"):
        status = "running"
        request = running["requestId"] if running else request
    elif queued:
        status, request = "queued", queued["requestId"]
    return {
        "status": status,
        "label": LABELS[status],
        "active": status in ACTIVE,
        "requestId": request,
        "updatedAt": state.get("updatedAt", queue.get("updatedAt")),
    }


def artifact_status(exists, id, validation=None, dataQuality=None):
    if not exists:
        if id in (
            "generation_state",
            "generation_queue",
            "artifact_contracts",
            "visual_validation",
            "validation",
        ):
            return "unknown"
        return "warning" if id == "events" else "failed"
    if id == "validation":
        return (
            "unknown" if validation is None else "healthy" if validation.get("passed") else "failed"
        )
    if id == "data_quality":
        status = (dataQuality or {}).get("status")
        if status in ("ok", "passed"):
            return "healthy"
        if status == "warning":
            return "warning"
    return "healthy"


def array(value):
    return value if isinstance(value, list) else []


def data_quality_summary(dataQuality=None, sources=None):
    quality, sources = dataQuality or {}, sources or {}
    datasets = array(quality.get("datasets"))
    warnings = len(array(quality.get("warnings"))) + sum(
        isinstance(item, dict) and item.get("status") == "warning"
        for item in array(quality.get("checks"))
    )
    source_count = next(
        (
            len(array(sources.get(key)))
            for key in ("sources", "datasets", "items")
            if array(sources.get(key))
        ),
        0,
    )
    source_count = source_count or sum(
        isinstance(item, dict) and isinstance(item.get("source"), str) and bool(item["source"])
        for item in datasets
    )
    status = (
        "healthy"
        if quality.get("status") in ("ok", "passed")
        else "warning"
        if quality.get("status") == "warning"
        else "unknown"
        if dataQuality is not None
        else "failed"
    )
    return {
        "status": status,
        "datasetCount": len(datasets),
        "warningCount": warnings,
        "sourceCount": source_count,
        "updatedAt": next(
            (
                quality[key]
                for key in ("created_at", "updatedAt")
                if isinstance(quality.get(key), str) and quality[key]
            ),
            None,
        ),
    }


def read_object(root, relative):
    try:
        value = json.loads(
            read_regular(controlled_path(root, relative), 8388608).decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        return value if isinstance(value, dict) else None
    except (ValueError, OSError, UnicodeError):
        return None


def read_events(root, limit=20):
    try:
        lines = (
            read_regular(controlled_path(root, ".data-agent/events.jsonl"), 33554432)
            .decode("utf-8", "replace")
            .splitlines()
        )
    except (ValueError, OSError):
        return []
    result = []
    for line in [line.strip() for line in lines if line.strip()][-limit:]:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result.append(value)
        except ValueError:
            pass
    return result


def check_count(report, status):
    return sum(
        isinstance(item, dict) and item.get("status") == status
        for item in array((report or {}).get("checks"))
    )


def report_summary(report, active, path=None, visual=False):
    report_value = report or {}
    passed = report_value.get("passed")
    status = (
        "unknown"
        if report is None
        else "healthy"
        if passed is True
        else "warning"
        if active
        else "failed"
    )
    if path and report is not None:
        status = (
            "healthy"
            if report_value.get("status") == "passed"
            else "warning"
            if active or report_value.get("status") == "warning"
            else "failed"
        )
    result = {
        "status": status,
        "passed": passed,
        "updatedAt": report_value.get("updatedAt") or report_value.get("createdAt"),
        "failedChecks": len(array(report_value.get("failures")))
        if visual
        else check_count(report, "failed"),
        "warningChecks": len(array(report_value.get("warnings")))
        if visual
        else check_count(report, "warning"),
    }
    if path:
        result["path"] = path
    return result


def delivery_segment(project, health, queue, repair, lifecycle, *, clock=None):
    status = lifecycle["status"]
    if status == "awaiting_input":
        return {
            "id": "at_risk",
            "label": "等待补充",
            "reason": "任务正在等待用户补齐关键信息，不属于生成失败或待修复。",
        }
    if status in ("queued", "running", "repairing"):
        return {
            "id": "at_risk",
            "label": lifecycle["label"],
            "reason": "生成链路仍在执行，完成后再按最新验证结果进入交付分层。",
        }
    if health["status"] == "healthy" or health["blockers"] == 0 and health["score"] >= 82:
        return {
            "id": "showcase",
            "label": "可演示",
            "reason": "关键产物、验证和页面交付状态可用于展示。",
        }
    active_queue = queue["running"] > 0 or queue["queued"] > 0
    inactive = project.get("last_active_at") or project.get("updated_at")
    if isinstance(inactive, str):
        try:
            inactive = datetime.fromisoformat(inactive.replace("Z", "+00:00"))
        except ValueError:
            inactive = None
    if inactive is not None and inactive.tzinfo is None:
        inactive = inactive.replace(tzinfo=UTC)
    days = (
        max(0, math.floor(((clock or datetime.now(UTC)) - inactive).total_seconds() / 86400))
        if inactive
        else None
    )
    if (
        health["status"] == "failed"
        and not active_queue
        and not repair
        and days is not None
        and days >= 7
    ):
        return {
            "id": "archive_candidate",
            "label": "归档候选",
            "reason": f"已 {days} 天未活跃且存在阻断项，建议从主可用性口径中分离。",
        }
    if health["status"] == "failed" or repair or active_queue:
        return {
            "id": "needs_repair",
            "label": "待修复",
            "reason": "仍有生成队列任务，需要等待完成后重新验收。"
            if active_queue
            else "存在验证、产物契约或视觉验收阻断项。",
        }
    return {
        "id": "at_risk",
        "label": "有风险",
        "reason": "产物可追踪但存在警告、未知验证或数据质量风险。"
        if health["warnings"] > 0 or health["status"] in ("warning", "unknown")
        else "状态不完整，建议补充验证报告。",
    }


def next_actions(artifacts, validation, quality, repair, contracts, visual, queue, lifecycle):
    active = {
        "awaiting_input": "补充当前任务缺失的标的、范围或时间信息，再继续生成。",
        "queued": "任务已进入生成队列，等待前序任务完成。",
        "running": "生成链路正在运行，完成后再按最新验证结果判断是否需要修复。",
        "repairing": "自动修复正在执行，等待修复后验证完成。",
    }
    if lifecycle["status"] in active:
        return [active[lifecycle["status"]]]
    actions = []
    if missing := [item["label"] for item in artifacts if not item["exists"]]:
        actions.append("补齐缺失产物：" + "、".join(missing[:3]) + "。")
    if validation["status"] == "failed":
        actions.append("重新运行自动验证，并按失败 check 生成修复指令。")
    elif validation["status"] == "unknown":
        actions.append("运行一次自动验证，生成 .data-agent/validation.json。")
    if quality["status"] == "warning":
        actions.append("检查 evidence/data_quality.json 中的数据缺口，并在页面结论中说明限制。")
    if repair:
        actions.append("处理 validation-repair-plan.json 中的修复步骤。")
    if contracts["status"] == "failed":
        actions.append(f"修复 {contracts['path']} 中的 {contracts['failedChecks']} 个失败契约。")
    if visual["status"] == "failed":
        actions.append(
            f"查看 {visual['path']} 和截图，修复 {visual['failedChecks']} 个视觉阻断项。"
        )
    if queue["queued"] > 0 or queue["running"] > 0:
        actions.append(
            f"生成队列中仍有 {queue['running']} 个运行中、{queue['queued']} 个排队任务。"
        )
    return actions or ["保持当前 workspace，后续变更后重新验证。"]


async def inspect_workspace(projects, project):
    root = projects.managed_path(project, must_exist=True)
    validation, repair, plan, quality, sources, contracts, visual = [
        read_object(root, path)
        for path in (
            ".data-agent/validation.json",
            ".data-agent/validation-repair-plan.json",
            ".data-agent/finance-run-plan.json",
            "evidence/data_quality.json",
            "evidence/sources.json",
            ".data-agent/artifact-contracts.json",
            ".data-agent/visual-validation.json",
        )
    ]
    state, queue = (
        GenerationState(root).read(),
        await queue_projection(projects.database, project["id"]),
    )
    lifecycle = derive_lifecycle(
        generationState=state, queue=queue, runPlanStatus=(plan or {}).get("status")
    )
    active = lifecycle["active"]
    artifacts = []
    for definition in json.loads((ROOT / "config/workspace-health-catalog.json").read_text())[
        "artifacts"
    ]:
        try:
            path = controlled_path(root, definition["path"])
            stat = path.stat() if path.is_file() else None
        except (OSError, ValueError):
            stat = None
        status = artifact_status(stat is not None, definition["id"], validation, quality)
        artifacts.append(
            {
                **definition,
                "exists": stat is not None,
                "status": "unknown" if active and status == "failed" else status,
                "updatedAt": iso(datetime.fromtimestamp(stat.st_mtime, UTC)) if stat else None,
                "summary": "已生成" if stat else "当前生命周期尚未生成" if active else "缺失",
            }
        )
    quality_summary = data_quality_summary(quality, sources)
    validation_summary = report_summary(validation, active)
    contracts_summary = report_summary(contracts, active, ".data-agent/artifact-contracts.json")
    visual_summary = report_summary(
        visual, active, ".data-agent/visual-validation.json", visual=True
    )
    queue_summary = {
        "activeRequestId": queue["activeRequestId"],
        "updatedAt": queue["updatedAt"],
        **{
            status: sum(item["status"] == status for item in queue["items"])
            for status in ("running", "queued", "failed")
        },
    }
    blockers = (
        0
        if active
        else sum(item["status"] == "failed" for item in artifacts)
        + sum(
            (item["failedChecks"] or 1) if item["status"] == "failed" else 0
            for item in (validation_summary, contracts_summary, visual_summary)
        )
    )
    warnings = (
        sum(item["status"] in ("warning", "unknown") for item in artifacts)
        + quality_summary["warningCount"]
        + sum(
            item["warningChecks"]
            for item in (validation_summary, contracts_summary, visual_summary)
        )
        + int(queue_summary["running"] + queue_summary["queued"] > 0)
    )
    worst = max(
        [
            item["status"]
            for item in (
                validation_summary,
                quality_summary,
                contracts_summary,
                visual_summary,
                *artifacts,
            )
        ],
        key=RANK.get,
    )
    status = (
        "warning"
        if active
        else "failed"
        if blockers or worst == "failed"
        else "warning"
        if warnings
        else "healthy"
    )
    summary = (
        "工作空间关键产物和验证状态正常。"
        if status == "healthy"
        else f"{blockers} 个阻断项需要处理。"
        if status == "failed"
        else f"{warnings} 个风险项需要关注。"
        if status == "warning"
        else "工作空间状态不完整，需要先补充验证报告。"
    )
    health = {
        "status": status,
        "score": max(0, min(100, 100 - blockers * 18 - warnings * 4)),
        "summary": summary,
        "blockers": blockers,
        "warnings": warnings,
    }
    needed = bool((repair or {}).get("steps"))
    return {
        "id": project["id"],
        "name": project["name"],
        "description": project["description"],
        "status": project["status"],
        "repoPath": os.path.relpath(root, ROOT),
        "preferredCli": project["preferred_cli"],
        "selectedModel": project["selected_model"],
        "quantCapabilityId": settings_record(project["settings"])
        .get("dataAgent", {})
        .get("capabilityId"),
        "previewUrl": project["preview_url"],
        **{
            key: iso(project[column]) if project[column] else None
            for key, column in (
                ("createdAt", "created_at"),
                ("updatedAt", "updated_at"),
                ("lastActiveAt", "last_active_at"),
            )
        },
        "health": health,
        "lifecycle": lifecycle,
        "deliverySegment": delivery_segment(project, health, queue_summary, needed, lifecycle),
        "validation": validation_summary,
        "dataQuality": quality_summary,
        "runPlan": {
            "status": (plan or {}).get("status"),
            "capabilityId": (plan or {}).get("capabilityId"),
            "symbols": (plan or {}).get("symbols", []),
            "updatedAt": (plan or {}).get("updatedAt"),
        },
        "generationQueue": queue_summary,
        "artifactContracts": contracts_summary,
        "visualValidation": visual_summary,
        "artifacts": artifacts,
        "events": read_events(root),
        "repairPlan": {
            "needed": needed,
            "stepCount": len(array((repair or {}).get("steps"))),
            "path": (repair or {}).get("repairPlanPath"),
        },
        "nextActions": next_actions(
            artifacts,
            validation_summary,
            quality_summary,
            needed,
            contracts_summary,
            visual_summary,
            queue_summary,
            lifecycle,
        ),
    }


async def workspace_dashboard(projects):
    rows = (
        await projects.database.fetch_all("SELECT * FROM projects ORDER BY created_at DESC")
        if degradation_config()["components"]["database"]["enabled"]
        else []
    )
    items = await asyncio.gather(*(inspect_workspace(projects, row) for row in rows))
    summary = {
        "total": len(items),
        **{status: sum(item["health"]["status"] == status for item in items) for status in RANK},
        "averageScore": math.floor(
            sum(item["health"]["score"] for item in items) / len(items) + 0.5
        )
        if items
        else 0,
    }
    active = [item for item in items if item["deliverySegment"]["id"] != "archive_candidate"]
    delivery = {
        key: sum(item["deliverySegment"]["id"] == segment for item in items)
        for key, segment in (
            ("showcase", "showcase"),
            ("atRisk", "at_risk"),
            ("needsRepair", "needs_repair"),
            ("archiveCandidates", "archive_candidate"),
        )
    }
    delivery.update(
        activeTotal=len(active),
        activeAverageScore=math.floor(
            sum(item["health"]["score"] for item in active) / len(active) + 0.5
        )
        if active
        else 0,
    )
    items.sort(key=lambda item: item["updatedAt"], reverse=True)
    items.sort(key=lambda item: RANK[item["health"]["status"]], reverse=True)
    return {
        "generatedAt": now(),
        "projectsDir": os.path.relpath(projects.root, ROOT),
        "summary": summary,
        "delivery": delivery,
        "lifecycle": {
            status: sum(item["lifecycle"]["status"] == status for item in items)
            for status in LABELS
        },
        "projects": items,
    }
