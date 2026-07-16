"""Read-only generation projections from PostgreSQL facts and managed workspace evidence."""

import asyncio
import math
import os
import re
from datetime import UTC, datetime, timedelta

from .config import ROOT
from .generation_state import GenerationState, now
from .icu_collation import collator
from .messages import js_length, js_slice
from .projects import settings_record
from .queue_projection import queue_projection
from .service_catalog import degradation_config
from .sessions import iso
from .settings import normalize_model
from .workspace_health import array, check_count, read_events, read_object

STAGES = {
    "request": "请求",
    "planning": "规划",
    "data": "数据",
    "tooling": "工具",
    "artifact": "产物",
    "validation": "验证",
    "repair": "修复",
    "completion": "完成",
}
RANK = {"success": 0, "unknown": 1, "pending": 2, "warning": 3, "error": 4}
EPOCH = "1970-01-01T00:00:00.000Z"


def compact(value, maximum=180):
    value = re.sub(r"\s+", " ", value).strip()
    return value if js_length(value) <= maximum else js_slice(value, 0, maximum - 1) + "…"


def date_iso(value):
    try:
        return (
            iso(datetime.fromisoformat(value.replace("Z", "+00:00")))
            if isinstance(value, str)
            else iso(value)
            if value
            else None
        )
    except (ValueError, TypeError):
        return None


def normalize_status(value):
    value = str(value or "").lower()
    for status, values in [
        (
            "success",
            {"success", "succeeded", "passed", "completed", "complete", "done", "healthy", "ok"},
        ),
        ("warning", {"warning", "warn", "needs_clarification", "stale", "risk"}),
        ("error", {"error", "failed", "failure", "blocked", "fatal"}),
        ("pending", {"pending", "processing", "running", "starting", "in_progress", "planned"}),
    ]:
        if value in values:
            return status
    return "unknown"


def workspace_stage(event):
    raw = (str(event.get("stage") or "") + " " + str(event.get("event_type") or "")).lower()
    for pattern, stage in [
        (r"queue", "request"),
        (r"intent|request|clarification", "planning" if "intent" in raw else "request"),
        (r"plan|planning|run_plan", "planning"),
        (r"data|quality|source|market|symbol|image|evidence", "data"),
        (r"validation|validate", "validation"),
        (r"repair|fix", "repair"),
        (r"complete|completed|finish|done", "completion"),
        (r"execution|artifact|visualization|page|dashboard", "artifact"),
    ]:
        if re.search(pattern, raw):
            return stage
    return "system"


def message_stage(kind, role, metadata):
    text = " ".join(
        [kind, role, *(str(metadata.get(key) or "") for key in ("toolName", "tool_name", "action"))]
    ).lower()
    return (
        "system"
        if kind == "error"
        else "tooling"
        if re.search(r"tool|bash|read|write|edit|apply_patch", text)
        else "request"
        if role == "user"
        else "planning"
        if re.search(r"plan|thinking", text)
        else "completion"
        if role == "assistant"
        else "system"
    )


def message_title(kind, role, metadata):
    tool = metadata.get("toolName") or metadata.get("tool_name")
    return (
        ("工具结果：" if kind == "tool_result" else "工具调用：") + tool
        if tool
        else "Agent 思考"
        if kind == "thinking"
        else "Agent 错误"
        if kind == "error"
        else "用户消息"
        if role == "user"
        else "Agent 回复"
        if role == "assistant"
        else "系统消息"
    )


def event(
    project,
    identity,
    source,
    stage,
    status,
    title,
    summary,
    timestamp,
    request=None,
    path=None,
    metadata=None,
):
    return {
        "id": identity,
        "projectId": project,
        "source": source,
        "stage": stage,
        "status": status,
        "title": title,
        "summary": summary,
        "timestamp": timestamp,
        "requestId": request,
        "artifactPath": path,
        "metadata": metadata or {},
    }


def workspace_events(project, events):
    titles = {
        "run_planned": "生成计划",
        "intent_clarification_required": "意图需要澄清",
        "data_prefetch_started": "数据预取开始",
        "data_prefetched": "数据预取完成",
        "data_quality_checked": "数据质量检查",
        "image_attachment_evidence_created": "图片证据建立",
        "agent_auto_completed": "Agent 自动完成",
        "validation_started": "验证开始",
        "validation_completed": "验证完成",
    }
    result = []
    for index, value in enumerate(events):
        timestamp = next(
            (
                date_iso(value.get(key))
                for key in ("created_at", "ts", "timestamp")
                if date_iso(value.get(key))
            ),
            EPOCH,
        )
        kind = value.get("event_type") or ""
        result.append(
            event(
                project,
                f"workspace:{project}:{index}:{timestamp}",
                "workspace_event",
                workspace_stage(value),
                normalize_status(value.get("status")),
                titles.get(kind, kind or value.get("stage") or "工作空间事件"),
                compact(value.get("summary") or value.get("stage") or "工作空间事件"),
                timestamp,
                value.get("run_id") or None,
                value.get("artifact_path") or None,
                {"eventType": kind, "rawStage": value.get("stage") or ""},
            )
        )
    return result


def state_events(project, state):
    if state is None:
        return []
    stages = {
        "request_received": "request",
        "planning": "planning",
        "data_prefetch": "data",
        "agent_execution": "artifact",
        "validation": "validation",
        "final_validation": "validation",
        "repair": "repair",
    }
    statuses = {
        "success": "success",
        "skipped": "success",
        "warning": "warning",
        "failed": "error",
        "running": "pending",
        "pending": "pending",
    }
    return [
        event(
            project,
            f"generation-state:{project}:{state['requestId']}:{step['id']}",
            "workspace_event",
            stages.get(step["id"], "completion"),
            statuses.get(step["status"], "unknown"),
            "状态机：" + step["label"],
            compact(step.get("summary") or step["status"]),
            step.get("completedAt") or step.get("startedAt") or state["updatedAt"],
            state["requestId"],
            ".data-agent/generation-state.json",
            {
                "runStatus": state["status"],
                "stepStatus": step["status"],
                "activeStep": state["activeStep"],
                "repairAttemptCount": state["repairAttemptCount"],
                **(step.get("metadata") or {}),
            },
        )
        for step in state.get("steps", [])
        if step.get("startedAt") or step.get("completedAt") or step.get("summary")
    ]


def queue_events(project, queue):
    result = []
    for item in (queue or {}).get("items", [])[:20]:
        prefix = f"generation-queue:{project}:{item['requestId']}:"
        path = ".data-agent/generation-queue.json"
        result.append(
            event(
                project,
                prefix + "queued",
                "workspace_event",
                "request",
                "pending" if item["status"] == "queued" else "success",
                "队列：已入队",
                compact(item.get("instructionPreview") or "生成任务已进入队列"),
                item["queuedAt"],
                item["requestId"],
                path,
                {
                    "queueStatus": item["status"],
                    "cliPreference": item.get("cliPreference"),
                    "selectedModel": normalize_model(item.get("selectedModel")),
                },
            )
        )
        if item.get("startedAt"):
            result.append(
                event(
                    project,
                    prefix + "started",
                    "workspace_event",
                    "tooling",
                    "pending" if item["status"] == "running" else "success",
                    "队列：开始执行",
                    "生成任务从队列取出并开始执行。",
                    item["startedAt"],
                    item["requestId"],
                    path,
                    {"queueStatus": item["status"]},
                )
            )
        if item.get("completedAt"):
            status = {
                "completed": "success",
                "failed": "error",
                "cancelled": "warning",
                "queued": "pending",
                "running": "pending",
            }.get(item["status"], "unknown")
            result.append(
                event(
                    project,
                    prefix + "finished",
                    "workspace_event",
                    "completion" if item["status"] == "completed" else "system",
                    status,
                    "队列：执行结束",
                    compact(item["errorMessage"])
                    if item.get("errorMessage")
                    else "生成任务状态：" + item["status"],
                    item["completedAt"],
                    item["requestId"],
                    path,
                    {"queueStatus": item["status"]},
                )
            )
    return result


def evidence_events(project, plan, validation, repair, contracts, visual):
    result = []
    if plan:
        result.append(
            event(
                project,
                f"run-plan:{project}:{plan.get('runId', plan.get('updatedAt'))}",
                "run_plan",
                "planning",
                "warning"
                if plan.get("status") == "needs_clarification"
                else normalize_status(plan.get("status")),
                "当前执行计划",
                compact(
                    plan.get("question")
                    or f"{plan.get('capabilityId')} · {'、'.join(plan.get('symbols', []))}"
                ),
                plan.get("updatedAt") or plan.get("createdAt"),
                plan.get("runId"),
                ".data-agent/finance-run-plan.json",
                {
                    key: plan[key]
                    for key in (
                        "capabilityId",
                        "requestedCapabilityId",
                        "executionCapabilityId",
                        "symbols",
                        "expectedArtifacts",
                    )
                    if key in plan
                },
            )
        )
    if validation:
        failed, warnings = check_count(validation, "failed"), check_count(validation, "warning")
        count = len(array(validation.get("checks")))
        result.append(
            event(
                project,
                f"validation-report:{project}:{validation.get('updatedAt')}",
                "validation",
                "validation",
                "success" if validation.get("passed") else "error",
                "最新验证报告",
                f"验证通过，{count} 项检查完成。"
                if validation.get("passed")
                else f"验证未通过：{failed} 项失败，{warnings} 项警告。",
                validation.get("updatedAt") or validation.get("createdAt"),
                validation.get("runId"),
                validation.get("reportPath"),
                {"failedChecks": failed, "warningChecks": warnings, "checkCount": count},
            )
        )
    if repair and repair.get("steps"):
        steps = repair["steps"]
        result.append(
            event(
                project,
                f"repair-plan:{project}:{repair.get('createdAt')}",
                "repair_plan",
                "repair",
                "warning",
                "验证修复计划",
                f"需要处理 {len(steps)} 个修复步骤："
                + compact("、".join(step.get("checkName", "") for step in steps), 130),
                repair.get("createdAt"),
                path=repair.get("repairPlanPath"),
                metadata={
                    "steps": [
                        {
                            key: step[key]
                            for key in ("checkId", "checkName", "summary")
                            if key in step
                        }
                        for step in steps
                    ]
                },
            )
        )
    if contracts:
        checks = array(contracts.get("checks"))
        result.append(
            event(
                project,
                f"artifact-contracts:{project}:{contracts.get('updatedAt')}",
                "validation",
                "validation",
                "success"
                if contracts.get("status") == "passed"
                else "warning"
                if contracts.get("status") == "warning"
                else "error",
                "产物 Schema 契约",
                f"契约检查通过，{len(checks)} 项结构检查完成。"
                if contracts.get("passed")
                else f"契约检查未通过：{check_count(contracts, 'failed')} 项失败。",
                contracts.get("updatedAt"),
                contracts.get("requestId"),
                ".data-agent/artifact-contracts.json",
                {
                    "failed": [check["id"] for check in checks if check.get("status") == "failed"],
                    "warnings": [
                        check["id"] for check in checks if check.get("status") == "warning"
                    ],
                },
            )
        )
    if visual:
        viewports, failures, warnings = (
            array(visual.get("viewports")),
            array(visual.get("failures")),
            array(visual.get("warnings")),
        )
        result.append(
            event(
                project,
                f"visual-validation:{project}:{visual.get('updatedAt')}",
                "validation",
                "validation",
                "success"
                if visual.get("status") == "passed"
                else "warning"
                if visual.get("status") == "warning"
                else "error",
                "视觉验收",
                f"视觉验收通过，覆盖 {len(viewports)} 个视口。"
                if visual.get("passed")
                else f"视觉验收未通过：{len(failures)} 个阻断项。",
                visual.get("updatedAt"),
                visual.get("requestId"),
                ".data-agent/visual-validation.json",
                {
                    "screenshots": [viewport.get("screenshotPath") for viewport in viewports],
                    "failures": failures,
                    "warnings": warnings,
                },
            )
        )
    return result


def db_events(project, requests, messages, tools):
    result = []
    for item in reversed(requests):
        result.append(
            event(
                project,
                "request:" + item["id"],
                "user_request",
                "request",
                normalize_status(item["status"]),
                "用户请求",
                compact(item["instruction"]),
                iso(item["created_at"]),
                item["id"],
                metadata={
                    "cliPreference": item["cli_preference"],
                    "completedAt": date_iso(item["completed_at"]),
                    "errorMessage": item["error_message"],
                },
            )
        )
    for item in reversed(messages):
        metadata = settings_record(item.get("metadata_json"))
        kind, role = item["message_type"], item["role"]
        if (
            kind == "chat"
            and role != "user"
            and not any(
                metadata.get(key) for key in ("toolName", "tool_name", "status", "action", "error")
            )
        ):
            continue
        title = message_title(kind, role, metadata)
        status = normalize_status(metadata.get("status"))
        if (
            kind == "error"
            or status == "unknown"
            and (metadata.get("error") or metadata.get("isError"))
        ):
            status = "error"
        elif status == "unknown":
            status = "success"
        result.append(
            event(
                project,
                "message:" + item["id"],
                "message",
                message_stage(kind, role, metadata),
                status,
                title,
                compact(item["content"] or title, 170),
                iso(item["created_at"]),
                item["request_id"],
                next(
                    (
                        metadata[key]
                        for key in ("path", "file_path", "filePath")
                        if isinstance(metadata.get(key), str) and metadata[key]
                    ),
                    None,
                ),
                {
                    "role": role,
                    "messageType": kind,
                    **{
                        public: item.get(column)
                        for public, column in (
                            ("cliSource", "cli_source"),
                            ("durationMs", "duration_ms"),
                            ("tokenCount", "token_count"),
                            ("costUsd", "cost_usd"),
                        )
                    },
                    **metadata,
                },
            )
        )
    for item in reversed(tools):
        metadata = settings_record(item["tool_input"])
        duration = f"，耗时 {item['duration_ms']}ms" if item.get("duration_ms") else ""
        output = compact(item["tool_output"], 120) if item.get("tool_output") else ""
        summary = (
            compact(item["error"], 200)
            if item.get("error")
            else "执行完成" + duration + "：" + output
            if output
            else "执行完成" + (duration or "。")
        )
        result.append(
            event(
                project,
                "tool:" + item["id"],
                "tool_usage",
                "tooling",
                "error" if item.get("error") else "success",
                "工具调用：" + item["tool_name"],
                summary,
                iso(item["created_at"]),
                path=next(
                    (
                        metadata[key]
                        for key in ("path", "file_path", "filePath")
                        if isinstance(metadata.get(key), str) and metadata[key]
                    ),
                    None,
                ),
                metadata={
                    "toolName": item["tool_name"],
                    "durationMs": item["duration_ms"],
                    "messageId": item["message_id"],
                    "input": metadata,
                },
            )
        )
    return result


def dedupe_timeline(events):
    # Explicit locale follows the source benchmark's persisted reference locale.
    events = sorted(
        events,
        key=lambda item: (date_iso(item["timestamp"]) or EPOCH, collator("en-US").key(item["id"])),
    )
    result, seen = [], set()
    for item in events:
        key = tuple(
            item[field]
            for field in ("source", "stage", "title", "summary", "timestamp", "artifactPath")
        )
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result[-220:]


def current_issue_window(timeline):
    checkpoint = next(
        (
            index
            for index in range(len(timeline) - 1, -1, -1)
            if timeline[index]["status"] == "success"
            and timeline[index]["stage"] in ("validation", "completion")
        ),
        -1,
    )
    return timeline[checkpoint + 1 :]


def build_stages(timeline):
    result = []
    for identity, label in STAGES.items():
        events = [item for item in timeline if item["stage"] == identity]
        result.append(
            {
                "id": identity,
                "label": label,
                "status": events[-1]["status"] if events else "unknown",
                "eventCount": len(events),
                "lastEventAt": events[-1]["timestamp"] if events else None,
            }
        )
    return result


def top_tools(tools, messages):
    grouped = {}
    values = [(item["tool_name"], item.get("error"), item.get("duration_ms")) for item in tools]
    if not tools:
        for item in messages:
            metadata = settings_record(item.get("metadata_json"))
            name = metadata.get("toolName") or metadata.get("tool_name")
            if item["message_type"] == "tool_use" and name:
                values.append(
                    (
                        name,
                        metadata.get("error") or metadata.get("isError"),
                        item.get("duration_ms"),
                    )
                )
    for name, error, duration in values:
        item = grouped.setdefault(
            name, {"count": 0, "errorCount": 0, "durationTotal": 0, "durationCount": 0}
        )
        item["count"] += 1
        item["errorCount"] += bool(error)
        if duration is not None:
            item["durationTotal"] += duration
            item["durationCount"] += 1
    result = [
        {
            "name": name,
            "count": item["count"],
            "errorCount": item["errorCount"],
            "averageDurationMs": math.floor(item["durationTotal"] / item["durationCount"] + 0.5)
            if item["durationCount"]
            else None,
        }
        for name, item in grouped.items()
    ]
    return sorted(result, key=lambda item: (-item["count"], -item["errorCount"]))[:8]


def trace_summary(status, errors, warnings, pending, latest, passed, repair_steps, awaiting):
    return (
        "任务正在等待用户补充关键信息，尚未进入失败或修复状态。"
        if awaiting
        else f"{errors or 1} 个当前错误事件阻断生成链路。"
        if status == "error"
        else f"验证修复计划待处理：{repair_steps} 步。"
        if repair_steps
        else f"{warnings or 1} 个当前风险事件需要关注。"
        if status == "warning"
        else "链路中仍有 pending 阶段，建议确认 Agent 是否仍在执行。"
        if pending
        else "生成、产物和验证链路已闭环。"
        if passed is True
        else f"最近停在「{latest['title']}」。"
        if latest
        else "暂无可观测事件，需要先触发一次生成或验证。"
    )


def trace_actions(current, validation, repair, awaiting):
    if awaiting:
        return ["先补齐用户意图澄清问题，再继续执行生成链路。"]
    error = next((item for item in reversed(current) if item["status"] == "error"), None)
    pending = next((item for item in reversed(current) if item["status"] == "pending"), None)
    actions = [f"优先处理 {error['title']}：{error['summary']}"] if error else []
    if repair["needed"]:
        actions.append(
            f"执行 {repair['path'] or '.data-agent/validation-repair-plan.json'} 中的 {repair['stepCount']} 个修复步骤。"
        )
    if validation["passed"] is False:
        actions.append("修复后重新运行自动验证，确认 validation.json 变为通过。")
    elif validation["passed"] is None:
        actions.append("补一次自动验证，把生成结果纳入平台质量门禁。")
    if pending and not error:
        actions.append(f"确认 pending 阶段是否仍在运行：{pending['title']}。")
    return actions[:4] or ["链路状态正常，后续变更后继续观察验证和修复事件。"]


async def inspect_trace(projects, project, *, summary_only=False, event_limit=220):
    root = projects.managed_path(project, must_exist=True)
    project_id, database = project["id"], projects.database
    requests, messages, tools, counts, queue = await asyncio.gather(
        database.fetch_all(
            "SELECT * FROM user_requests WHERE project_id=%s ORDER BY created_at DESC LIMIT %s",
            (project_id, 5 if summary_only else 40),
        ),
        database.fetch_all(
            "SELECT * FROM messages WHERE project_id=%s ORDER BY created_at DESC LIMIT %s",
            (project_id, 16 if summary_only else 180),
        ),
        database.fetch_all(
            "SELECT * FROM tool_usages WHERE project_id=%s ORDER BY created_at DESC LIMIT %s",
            (project_id, 12 if summary_only else 140),
        ),
        database.fetch_one(
            "SELECT (SELECT count(*) FROM user_requests WHERE project_id=%s) AS requests,(SELECT count(*) FROM tool_usages WHERE project_id=%s) AS tools",
            (project_id, project_id),
        ),
        queue_projection(database, project_id),
    )
    plan, validation, repair, contracts, visual = [
        read_object(root, path)
        for path in (
            ".data-agent/finance-run-plan.json",
            ".data-agent/validation.json",
            ".data-agent/validation-repair-plan.json",
            ".data-agent/artifact-contracts.json",
            ".data-agent/visual-validation.json",
        )
    ]
    state = GenerationState(root).read()
    timeline = dedupe_timeline(
        db_events(project_id, requests, messages, tools)
        + workspace_events(project_id, read_events(root, 20 if summary_only else event_limit))
        + evidence_events(project_id, plan, validation, repair, contracts, visual)
        + state_events(project_id, state)
        + queue_events(project_id, queue)
    )[-max(20, min(220, event_limit)) :]
    current, latest = current_issue_window(timeline), timeline[-1] if timeline else None
    awaiting = (
        state.get("status") == "needs_clarification"
        if state
        else (plan or {}).get("status") == "needs_clarification"
    )
    repair_date, validation_date = (
        date_iso((repair or {}).get("createdAt")),
        date_iso((validation or {}).get("updatedAt") or (validation or {}).get("createdAt")),
    )
    needed = bool(
        not awaiting
        and (repair or {}).get("steps")
        and (
            not (validation or {}).get("passed")
            or repair_date
            and validation_date
            and repair_date > validation_date
        )
    )
    repair_summary = {
        "needed": needed,
        "stepCount": len(array((repair or {}).get("steps"))),
        "path": (repair or {}).get("repairPlanPath"),
        "createdAt": (repair or {}).get("createdAt"),
    }
    validation_summary = {
        "status": "unknown"
        if validation is None
        else "success"
        if validation.get("passed")
        else "error",
        "passed": (validation or {}).get("passed"),
        "updatedAt": (validation or {}).get("updatedAt") or (validation or {}).get("createdAt"),
        "failedChecks": check_count(validation, "failed"),
        "warningChecks": check_count(validation, "warning"),
    }
    queue_summary = {
        "activeRequestId": queue["activeRequestId"],
        "updatedAt": queue["updatedAt"],
        "path": ".data-agent/generation-queue.json",
        "items": [
            {
                key: normalize_model(item[key]) if key == "selectedModel" else value
                for key, value in item.items()
                if key not in ("id", "projectId")
            }
            for item in queue["items"][:12]
        ],
        **{
            status: sum(item["status"] == status for item in queue["items"])
            for status in ("running", "queued", "failed")
        },
    }
    contracts_summary = {
        "status": "unknown"
        if contracts is None
        else "success"
        if contracts.get("status") == "passed"
        else "warning"
        if contracts.get("status") == "warning"
        else "error",
        "passed": (contracts or {}).get("passed"),
        "failedChecks": check_count(contracts, "failed"),
        "warningChecks": check_count(contracts, "warning"),
        "updatedAt": (contracts or {}).get("updatedAt"),
        "path": ".data-agent/artifact-contracts.json",
        "checks": [
            {
                key: item[key]
                for key in ("id", "label", "path", "required", "status", "summary", "details")
                if key in item
            }
            for item in array((contracts or {}).get("checks"))
        ],
    }
    viewports = array((visual or {}).get("viewports"))
    visual_summary = {
        "status": "unknown"
        if visual is None
        else "success"
        if visual.get("status") == "passed"
        else "warning"
        if visual.get("status") == "warning"
        else "error",
        "passed": (visual or {}).get("passed"),
        "failedChecks": len(array((visual or {}).get("failures"))),
        "warningChecks": len(array((visual or {}).get("warnings"))),
        "updatedAt": (visual or {}).get("updatedAt"),
        "path": ".data-agent/visual-validation.json",
        "screenshots": [viewport.get("screenshotPath") for viewport in viewports],
        "previewUrl": (visual or {}).get("previewUrl"),
        "viewports": [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "width",
                    "height",
                    "screenshotPath",
                    "status",
                    "failures",
                    "warnings",
                )
            }
            for item in viewports
        ],
        "failures": array((visual or {}).get("failures")),
        "warnings": array((visual or {}).get("warnings")),
    }
    current_counts = {
        status: sum(item["status"] == status for item in current)
        for status in ("error", "warning", "pending")
    }
    status = (
        "warning"
        if awaiting
        else "error"
        if validation_summary["passed"] is False
        or current_counts["error"]
        or contracts_summary["status"] == "error"
        or visual_summary["status"] == "error"
        else "warning"
        if needed
        or current_counts["warning"]
        or contracts_summary["status"] == "warning"
        or visual_summary["status"] == "warning"
        else "pending"
        if current_counts["pending"]
        or project["status"] == "running"
        or queue_summary["running"] + queue_summary["queued"]
        else "success"
        if timeline
        else "unknown"
    )
    active = next(
        (
            item["stage"]
            for item in reversed(current)
            if item["status"] in ("error", "warning", "pending")
        ),
        latest["stage"] if latest else "system",
    )
    trace = {
        "status": status,
        "summary": trace_summary(
            status,
            current_counts["error"],
            current_counts["warning"],
            current_counts["pending"],
            latest,
            validation_summary["passed"],
            repair_summary["stepCount"] if needed else 0,
            awaiting,
        ),
        "activeStage": active,
        "lastEventAt": latest["timestamp"] if latest else None,
        "eventCount": len(timeline),
        **{
            key: sum(item["status"] == value for item in timeline)
            for key, value in (
                ("errorCount", "error"),
                ("warningCount", "warning"),
                ("pendingCount", "pending"),
            )
        },
        "requestCount": counts["requests"],
        "toolCallCount": counts["tools"]
        or sum(item["message_type"] == "tool_use" for item in messages),
        "validationPassed": validation_summary["passed"],
        "repairStepCount": repair_summary["stepCount"] if needed else 0,
    }
    latest_request = requests[0] if requests else None
    return {
        "id": project_id,
        "name": project["name"],
        "description": project["description"],
        "status": project["status"],
        "repoPath": os.path.relpath(root, ROOT),
        "preferredCli": project["preferred_cli"],
        "selectedModel": normalize_model(project["selected_model"]),
        "previewUrl": project["preview_url"],
        **{
            key: date_iso(project[column])
            for key, column in (
                ("createdAt", "created_at"),
                ("updatedAt", "updated_at"),
                ("lastActiveAt", "last_active_at"),
            )
        },
        "trace": trace,
        "runPlan": {
            **{
                key: (plan or {}).get(key)
                for key in (
                    "status",
                    "capabilityId",
                    "requestedCapabilityId",
                    "executionCapabilityId",
                    "updatedAt",
                )
            },
            "symbols": (plan or {}).get("symbols", []),
            "expectedArtifacts": (plan or {}).get("expectedArtifacts", []),
        },
        "stages": build_stages(timeline),
        "timeline": timeline,
        "generationState": {
            **{
                key: state.get(key)
                for key in (
                    "status",
                    "activeStep",
                    "repairAttemptCount",
                    "maxRepairAttempts",
                    "updatedAt",
                    "error",
                )
            },
            "path": ".data-agent/generation-state.json",
            "steps": [
                {
                    key: item.get(key)
                    for key in ("id", "label", "status", "startedAt", "completedAt", "summary")
                }
                for item in state.get("steps", [])
            ],
        }
        if state
        else None,
        "generationQueue": queue_summary,
        "artifactContracts": contracts_summary,
        "visualValidation": visual_summary,
        "latestRequest": {
            "id": latest_request["id"],
            "status": latest_request["status"],
            "instruction": latest_request["instruction"],
            "createdAt": date_iso(latest_request["created_at"]),
            "completedAt": date_iso(latest_request["completed_at"]),
            "errorMessage": latest_request["error_message"],
        }
        if latest_request
        else None,
        "validation": validation_summary,
        "repairPlan": repair_summary,
        "topTools": top_tools(tools, messages),
        "nextActions": trace_actions(current, validation_summary, repair_summary, awaiting),
    }


async def generation_dashboard(projects, *, summary_only=False, event_limit=220):
    event_limit = 40 if summary_only else max(20, min(220, event_limit))
    rows = (
        await projects.database.fetch_all("SELECT * FROM projects ORDER BY last_active_at DESC")
        if degradation_config()["components"]["database"]["enabled"]
        else []
    )
    items = await asyncio.gather(
        *(
            inspect_trace(projects, row, summary_only=summary_only, event_limit=event_limit)
            for row in rows
        )
    )
    day_ago = iso(datetime.now(UTC) - timedelta(days=1))
    summary = {
        "total": len(items),
        **{
            key: sum(item["trace"]["status"] == status for item in items)
            for key, status in (
                ("healthy", "success"),
                ("warning", "warning"),
                ("failed", "error"),
                ("running", "pending"),
                ("unknown", "unknown"),
            )
        },
        "eventsLast24h": sum(
            (date_iso(event["timestamp"]) or EPOCH) >= day_ago
            for item in items
            for event in item["timeline"]
        ),
        "toolCalls": sum(item["trace"]["toolCallCount"] for item in items),
        "requests": sum(item["trace"]["requestCount"] for item in items),
    }
    items.sort(key=lambda item: item["trace"]["lastEventAt"] or item["updatedAt"], reverse=True)
    items.sort(key=lambda item: RANK[item["trace"]["status"]], reverse=True)
    return {
        "generatedAt": now(),
        "projectsDir": os.path.relpath(projects.root, ROOT),
        "summary": summary,
        "projects": [] if summary_only else items,
    }
