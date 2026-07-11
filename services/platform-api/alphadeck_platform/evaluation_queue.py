"""PostgreSQL-authoritative benchmark queue, exclusive claims and atomic schedules."""

import json
import math
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from .config import ROOT
from .finance_values import js_string
from .generation_lease import database_now
from .quota import one
from .runtime_store import insert, snake
from .sessions import iso
from .settings import DEFAULT_MODEL

LEASE_MS = 60000
QUEUE_FIELDS = [
    "id",
    "status",
    "createdAt",
    "startedAt",
    "finishedAt",
    "cli",
    "model",
    "reasoningEffort",
    "evaluatorId",
    "concurrency",
    "repeat",
    "mode",
    "selectedCases",
    "limit",
    "keepProjects",
    "reportId",
    "reportPath",
    "logPath",
    "pid",
    "exitCode",
    "error",
]


def numeric(value, fallback=0):
    return value if type(value) in {int, float} and math.isfinite(value) else fallback


def strings(value):
    return [text for item in value if (text := js_string(item))] if isinstance(value, list) else []


def date(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) if value else None
        )
    except (ValueError, AttributeError):
        return None


def bounded(value, default, maximum):
    return min(maximum, max(1, math.floor(numeric(value, default))))


def queue_view(row):
    result = {key: row.get(snake(key)) for key in QUEUE_FIELDS}
    for key in ("createdAt", "startedAt", "finishedAt"):
        result[key] = iso(date(result[key])) if result[key] else None
    result.update(
        status=result["status"]
        if result["status"] in {"queued", "running", "passed", "failed", "cancelled"}
        else "failed",
        evaluatorId=(
            result["evaluatorId"].strip() if isinstance(result["evaluatorId"], str) else ""
        )
        or "rule-strict",
        concurrency=bounded(result["concurrency"], 1, 16),
        repeat=bounded(result["repeat"], 1, 5),
        mode="e2e" if result["mode"] == "e2e" else "contract",
        selectedCases=strings(result["selectedCases"]),
    )
    return result


def virtual_item(options=None, *, now=None):
    options = options or {}
    evaluator = options.get("evaluatorId")
    evaluator = (
        evaluator.strip() if isinstance(evaluator, str) and evaluator.strip() else "rule-strict"
    )
    catalog = json.loads((ROOT / "config/evaluation-catalog.json").read_text())
    definition = next((item for item in catalog["evaluators"] if item["id"] == evaluator), None)
    if definition is None:
        raise ValueError("未知评测器：" + evaluator)
    mode = "e2e" if options.get("mode") == "e2e" else "contract"
    if mode not in definition["supportedModes"]:
        raise ValueError(f"{evaluator} 不支持 {mode} 模式。")
    repeat = options.get("repeat")
    if type(repeat) not in {int, float}:
        match = re.match(r"^[+-]?[0-9]+", js_string(repeat if repeat is not None else "").lstrip())
        repeat = int(match[0]) if match else 1
    limit = numeric(options.get("limit"), 0)
    return {
        "id": "eval-run-simulation",
        "status": "queued",
        "createdAt": iso(now or datetime.now(UTC)),
        "startedAt": None,
        "finishedAt": None,
        "cli": "pi",
        "model": catalog["defaultModel"],
        "reasoningEffort": "",
        "evaluatorId": evaluator,
        "concurrency": bounded(options.get("concurrency"), 1, 16),
        "repeat": bounded(repeat, 1, 5),
        "mode": mode,
        "selectedCases": strings(options.get("selectedCases")),
        "limit": math.floor(limit) if limit > 0 else None,
        "keepProjects": bool(options.get("keepProjects")),
        "reportId": None,
        "reportPath": None,
        "logPath": None,
        "pid": None,
        "exitCode": None,
        "error": None,
    }


def schedule_view(row=None):
    if row is None:
        return {
            "enabled": False,
            "intervalHours": 24,
            "cli": "pi",
            "model": DEFAULT_MODEL,
            "reasoningEffort": "",
            "selectedCases": [],
            "limit": None,
            "keepProjects": False,
            "nextRunAt": None,
            "lastRunAt": None,
            "lastQueuedRunId": None,
            "updatedAt": None,
        }
    result = {key: row[snake(key)] for key in schedule_view()}
    result["selectedCases"] = strings(result["selectedCases"])
    for key in ("nextRunAt", "lastRunAt", "updatedAt"):
        result[key] = iso(result[key]) if result[key] else None
    return result


class EvaluationQueue:
    def __init__(self, database):
        self.database = database

    async def list(self):
        return [
            queue_view(row)
            for row in await self.database.fetch_all(
                "SELECT * FROM eval_queue_items ORDER BY created_at DESC LIMIT 80"
            )
        ]

    async def enqueue(self, item, *, connection=None):
        values = {snake(key): item[key] for key in QUEUE_FIELDS}
        for key in ("created_at", "started_at", "finished_at"):
            values[key] = date(values[key])
        values.update(selected_cases=Jsonb(values["selected_cases"]), updated_at=datetime.now(UTC))
        if connection:
            return queue_view(await insert(connection, "eval_queue_items", values))
        async with self.database.transaction() as tx:
            return queue_view(await insert(tx, "eval_queue_items", values))

    async def start(self, options=None):
        item = {**virtual_item(options), "id": "eval-run-" + str(uuid4())}
        return await self.enqueue(item)

    async def claim(self):
        async with self.database.transaction() as tx:
            await tx.execute("SELECT pg_advisory_xact_lock(hashtext('alphadeck:eval-queue'))")
            now = await database_now(tx)
            await tx.execute(
                "UPDATE eval_queue_items SET status='failed',finished_at=%s,pid=NULL,lease_token=NULL,lease_expires_at=NULL,error='评测 Worker 租约已失效；请检查日志后重新发起任务。',updated_at=%s WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<=%s)",
                (now, now, now),
            )
            if await one(
                tx, "SELECT id FROM eval_queue_items WHERE lease_expires_at>%s LIMIT 1", (now,)
            ):
                return None
            row = await one(
                tx,
                "SELECT * FROM eval_queue_items WHERE status='queued' ORDER BY created_at,id LIMIT 1 FOR UPDATE",
            )
            if row is None:
                return None
            token = str(uuid4())
            claimed = await one(
                tx,
                "UPDATE eval_queue_items SET status='running',started_at=%s,finished_at=NULL,error=NULL,lease_token=%s,lease_expires_at=%s,attempt_count=attempt_count+1,updated_at=%s WHERE id=%s AND status='queued' RETURNING *",
                (now, token, now + timedelta(milliseconds=LEASE_MS), now, row["id"]),
            )
            return (
                {
                    "item": queue_view(claimed),
                    "leaseToken": token,
                    "attemptCount": claimed["attempt_count"],
                }
                if claimed
                else None
            )

    async def owned(self, claim, assignments, values=()):
        async with self.database.transaction() as tx:
            result = await tx.execute(
                "UPDATE eval_queue_items SET "
                + assignments
                + ",updated_at=clock_timestamp() WHERE id=%s AND lease_token=%s AND status='running' AND lease_expires_at>clock_timestamp()",
                (*values, claim["item"]["id"], claim["leaseToken"]),
            )
            return result.rowcount == 1

    async def heartbeat(self, claim):
        return await self.owned(claim, "lease_expires_at=clock_timestamp()+interval '60 seconds'")

    async def record_process(self, claim, pid, path):
        return await self.owned(claim, "pid=%s,log_path=%s", (pid, path))

    async def finish(self, claim, result):
        if result["status"] not in {"passed", "failed"}:
            raise ValueError("Unsupported evaluation terminal status")
        return await self.owned(
            claim,
            "status=%s,exit_code=%s,error=%s,report_id=%s,report_path=%s,finished_at=clock_timestamp(),pid=NULL,lease_token=NULL,lease_expires_at=NULL",
            (
                result["status"],
                result.get("exitCode"),
                result.get("error"),
                result.get("reportId"),
                result.get("reportPath"),
            ),
        )

    async def cancel(self, identity):
        async with self.database.transaction() as tx:
            await tx.execute(
                "UPDATE eval_queue_items SET status='cancelled',finished_at=clock_timestamp(),error='用户取消评测任务。',updated_at=clock_timestamp() WHERE id=%s AND status IN ('queued','running')",
                (identity,),
            )
            row = await one(tx, "SELECT * FROM eval_queue_items WHERE id=%s", (identity,))
            if row is None:
                raise ValueError("未找到评测队列任务。")
            return queue_view(row)

    async def release_cancelled(self, claim):
        async with self.database.transaction() as tx:
            await tx.execute(
                "UPDATE eval_queue_items SET lease_token=NULL,lease_expires_at=NULL,pid=NULL,updated_at=clock_timestamp() WHERE id=%s AND lease_token=%s AND status='cancelled'",
                (claim["item"]["id"], claim["leaseToken"]),
            )

    async def read_schedule(self, tx):
        return schedule_view(await one(tx, "SELECT * FROM eval_schedules WHERE id='default'"))

    async def write_schedule(self, tx, value):
        row = await one(
            tx,
            """INSERT INTO eval_schedules (id,enabled,interval_hours,cli,model,reasoning_effort,selected_cases,"limit",keep_projects,next_run_at,last_run_at,last_queued_run_id,updated_at)
            VALUES ('default',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())
            ON CONFLICT (id) DO UPDATE SET enabled=EXCLUDED.enabled,interval_hours=EXCLUDED.interval_hours,cli=EXCLUDED.cli,model=EXCLUDED.model,reasoning_effort=EXCLUDED.reasoning_effort,
            selected_cases=EXCLUDED.selected_cases,"limit"=EXCLUDED."limit",keep_projects=EXCLUDED.keep_projects,next_run_at=EXCLUDED.next_run_at,last_run_at=EXCLUDED.last_run_at,last_queued_run_id=EXCLUDED.last_queued_run_id,updated_at=clock_timestamp() RETURNING *""",
            (
                value["enabled"],
                value["intervalHours"],
                value["cli"],
                value["model"],
                value["reasoningEffort"],
                Jsonb(value["selectedCases"]),
                value["limit"],
                value["keepProjects"],
                date(value["nextRunAt"]),
                date(value["lastRunAt"]),
                value["lastQueuedRunId"],
            ),
        )
        return schedule_view(row)

    async def update_schedule(self, options):
        async with self.database.transaction() as tx:
            await tx.execute("SELECT pg_advisory_xact_lock(hashtext('alphadeck:eval-schedule'))")
            current, now = await self.read_schedule(tx), await database_now(tx)
            hours = numeric(options.get("intervalHours"), 0)
            hours = min(168, max(1, math.floor(hours))) if hours > 0 else current["intervalHours"]
            enabled = (
                options["enabled"] if type(options.get("enabled")) is bool else current["enabled"]
            )
            next_run = (
                options.get("nextRunAt")
                if "nextRunAt" in options
                else (current["nextRunAt"] or iso(now + timedelta(hours=hours)))
                if enabled
                else None
            )
            limit = numeric(options.get("limit"), 0)
            return await self.write_schedule(
                tx,
                {
                    **current,
                    "enabled": enabled,
                    "intervalHours": hours,
                    "cli": "pi",
                    "model": DEFAULT_MODEL,
                    "reasoningEffort": "",
                    "selectedCases": strings(options["selectedCases"])
                    if isinstance(options.get("selectedCases"), list)
                    else current["selectedCases"],
                    "limit": None
                    if "limit" in options and options["limit"] is None
                    else math.floor(limit)
                    if limit > 0
                    else current["limit"],
                    "keepProjects": options["keepProjects"]
                    if type(options.get("keepProjects")) is bool
                    else current["keepProjects"],
                    "nextRunAt": next_run,
                },
            )

    async def check_schedule(self):
        async with self.database.transaction() as tx:
            await tx.execute("SELECT pg_advisory_xact_lock(hashtext('alphadeck:eval-schedule'))")
            schedule, now = await self.read_schedule(tx), await database_now(tx)
            if (
                not schedule["enabled"]
                or not schedule["nextRunAt"]
                or date(schedule["nextRunAt"]) > now
            ):
                return {"queued": False, "schedule": schedule, "item": None}
            item = {
                **virtual_item(
                    {key: schedule[key] for key in ("selectedCases", "limit", "keepProjects")},
                    now=now,
                ),
                "id": "eval-run-" + str(uuid4()),
                **{key: schedule[key] for key in ("cli", "model", "reasoningEffort")},
            }
            item = await self.enqueue(item, connection=tx)
            updated = await self.write_schedule(
                tx,
                {
                    **schedule,
                    "lastRunAt": iso(now),
                    "lastQueuedRunId": item["id"],
                    "nextRunAt": iso(now + timedelta(hours=schedule["intervalHours"])),
                },
            )
            return {"queued": True, "schedule": updated, "item": item}
