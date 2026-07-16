import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from .provenance import js_json
from .resource_lock import resource_lock, sync_directory
from .skill_integrity import read_regular
from .workspace import workspace_file, write_atomic

RELATIVE = ".data-agent/generation-state.json"
LABELS = {
    "request_received": "接收请求",
    "planning": "生成计划",
    "data_prefetch": "数据预取",
    "agent_execution": "Agent 执行",
    "validation": "自动验证",
    "repair": "自动修复",
    "final_validation": "修复后验证",
    "preview": "持久看板预览",
    "evidence_verification": "独立证据验收",
    "completed": "完成",
}
STEP_STATUSES = {"pending", "running", "success", "warning", "failed", "skipped"}
RUN_STATUSES = {
    "pending",
    "running",
    "needs_clarification",
    "refused",
    "repairing",
    "completed",
    "failed",
    "cancelled",
}


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def max_repairs():
    try:
        value = int(os.getenv("ALPHADECK_MAX_VALIDATION_REPAIR_ATTEMPTS", ""))
        return value if value > 0 else 3
    except ValueError:
        return 3


def initial(
    project,
    request,
    timestamp,
    *,
    instruction="",
    cli=None,
    model=None,
    maximum=None,
    active="request_received",
):
    return {
        "schemaVersion": 1,
        "projectId": project,
        "requestId": request,
        "status": "running",
        "activeStep": active,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "completedAt": None,
        "originalInstruction": instruction,
        "cliPreference": cli,
        "selectedModel": model,
        "repairAttemptCount": 0,
        "maxRepairAttempts": max_repairs() if maximum is None else maximum,
        "steps": [
            {
                "id": key,
                "label": label,
                "status": "running" if key == "request_received" else "pending",
                "startedAt": timestamp if key == "request_received" else None,
                "completedAt": None,
                "summary": "",
            }
            for key, label in LABELS.items()
        ],
        "error": None,
    }


def derive_status(previous, step, status, override=None):
    if override:
        return override
    if status == "failed":
        return "failed"
    if step == "repair" and status == "running":
        return "repairing"
    if step == "completed" and status == "success":
        return "completed"
    return previous if previous in {"needs_clarification", "refused"} else "running"


def merge_steps(state, step, status, summary, timestamp, metadata=None):
    output, found = [], False
    for existing in state["steps"]:
        item = deepcopy(existing)
        if (
            item["id"] == "request_received"
            and step != "request_received"
            and item["status"] == "running"
        ):
            item.update(
                status="success",
                completedAt=item["completedAt"] or timestamp,
                summary=item["summary"] or "请求已接收。",
            )
        elif item["id"] == step:
            found = True
            item.update(
                status=status,
                startedAt=item["startedAt"] or timestamp,
                completedAt=timestamp
                if status in {"success", "warning", "failed", "skipped"}
                else item["completedAt"],
                summary=summary,
            )
            if metadata is not None:
                item["metadata"] = {**item.get("metadata", {}), **metadata}
        output.append(item)
    if not found:
        output.append(
            {
                "id": step,
                "label": LABELS[step],
                "status": status,
                "startedAt": timestamp,
                "completedAt": timestamp
                if status in {"success", "warning", "failed", "skipped"}
                else None,
                "summary": summary,
                **({"metadata": metadata} if metadata is not None else {}),
            }
        )
    return output


class GenerationState:
    def __init__(self, root, *, authority=None, clock=now):
        self.root, self.authority, self.clock = Path(root).resolve(strict=True), authority, clock

    def read(self):
        try:
            lexical = self.root / RELATIVE
            if lexical.resolve() != lexical:
                raise ValueError("Generation state cannot contain symbolic links")
            state = json.loads(read_regular(lexical, 2 * 1024 * 1024))
            return state if isinstance(state, dict) and state.get("schemaVersion") == 1 else None
        except (FileNotFoundError, ValueError):
            return None

    def _write(self, state):
        if (self.root / RELATIVE).resolve() != self.root / RELATIVE:
            raise ValueError("Generation state cannot contain symbolic links")
        target = write_atomic(self.root, RELATIVE, (js_json(state, indent=2) + "\n").encode())
        sync_directory(target.parent)

    def _event(self, state, kind, stage, status, summary):
        if (
            self.root / ".data-agent/events.jsonl"
        ).resolve() != self.root / ".data-agent/events.jsonl":
            raise ValueError("Generation event log cannot contain symbolic links")
        target = workspace_file(self.root, ".data-agent/events.jsonl")
        target.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "event_type": kind,
            "stage": stage,
            "status": status,
            "run_id": state["requestId"],
            "artifact_path": RELATIVE,
            "summary": summary,
            "created_at": state["updatedAt"],
        }
        data = (js_json(event) + "\n").encode()
        if len(data) > 262144:
            raise ValueError("Workspace progress event exceeds its byte budget")
        with os.fdopen(
            os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600
            ),
            "ab",
        ) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    async def _mutate(self, project, request, operation):
        async with resource_lock(
            self.root,
            {"purpose": "workspace_write", "projectId": project, "requestId": request},
            recover_dead_local_owner=True,
        ):
            if self.authority:
                return await self.authority(operation)
            return await operation(None)

    async def start(self, project, request, instruction, *, cli=None, model=None, maximum=None):
        async def operation(_tx):
            existing = self.read()
            if existing and existing["requestId"] == request and existing["status"] == "cancelled":
                return existing
            timestamp = self.clock()
            state = initial(
                project,
                request,
                timestamp,
                instruction=instruction,
                cli=cli,
                model=model,
                maximum=maximum,
            )
            self._write(state)
            self._event(
                state, "generation_state_started", "state_machine", "pending", "生成状态机已启动。"
            )
            return state

        return await self._mutate(project, request, operation)

    def _update(self, project, request, step, status, summary, metadata, run_status, error_message):
        existing = self.read()
        if (
            existing
            and existing["requestId"] == request
            and existing["status"] == "cancelled"
            and run_status != "cancelled"
        ):
            return existing
        timestamp = self.clock()
        state = (
            existing
            if existing and existing["requestId"] == request
            else initial(project, request, timestamp, active=step)
        )
        next_status = derive_status(state["status"], step, status, run_status)
        state = {
            **state,
            "status": next_status,
            "activeStep": step,
            "updatedAt": timestamp,
            "completedAt": timestamp
            if next_status in {"completed", "failed", "cancelled", "refused"}
            else None,
            "steps": merge_steps(state, step, status, summary, timestamp, metadata),
            "error": {
                "step": step,
                "message": error_message if error_message is not None else summary,
            }
            if error_message or status == "failed"
            else None
            if next_status == "completed"
            else state["error"],
        }
        self._write(state)
        self._event(
            state,
            "generation_state_updated",
            step,
            {"failed": "error", "warning": "warning", "running": "pending"}.get(status, "success"),
            LABELS[step] + "：" + summary,
        )
        return state

    async def update(
        self,
        project,
        request,
        step,
        status,
        summary,
        *,
        metadata=None,
        run_status=None,
        error_message=None,
    ):
        if (
            step not in LABELS
            or status not in STEP_STATUSES
            or (run_status is not None and run_status not in RUN_STATUSES)
        ):
            raise ValueError("Invalid generation step or status")

        async def operation(_tx):
            return self._update(
                project, request, step, status, summary, metadata, run_status, error_message
            )

        return await self._mutate(project, request, operation)

    async def increment_repair(self, project, request):
        async def operation(_tx):
            state = self.read()
            if not state or state["requestId"] != request:
                state = self._update(
                    project, request, "repair", "running", "开始自动修复。", None, "repairing", None
                )
            if state["status"] != "cancelled":
                state["repairAttemptCount"] += 1
                state["updatedAt"] = self.clock()
                self._write(state)
            return state["repairAttemptCount"]

        return await self._mutate(project, request, operation)
