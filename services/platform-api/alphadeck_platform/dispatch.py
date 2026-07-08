import hashlib
import json
import os
import re
from datetime import timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from .generation_lease import database_now
from .messages import js_length, js_slice
from .quota import one, structural_capacity, utc
from .sessions import iso

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
FORBIDDEN = re.compile(
    r"(?:^|_)(?:api_?key|authorization|cookie|password|secret|token)(?:$|_)", re.I
)


class DispatchError(Exception):
    def __init__(self, code, message, project=None, request=None):
        self.code, self.message, self.project, self.request = code, message, project, request
        super().__init__(message)


def identifier(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > 256
        or "\n" in value
        or "\r" in value
    ):
        raise DispatchError(
            "GENERATION_DISPATCH_INVALID", "Identifiers must be bounded single-line strings."
        )


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise DispatchError(
            "GENERATION_DISPATCH_INVALID", "Numeric dispatch option is outside its supported range."
        )


def configured(name, default, minimum, maximum):
    match = re.match(r"^[+-]?\d+", os.getenv(name, "").lstrip())
    result = int(match[0]) if match else default
    result = result or default
    integer(result, minimum, maximum)
    return result


def envelope(value):
    def verify(nested, depth=0):
        if depth > 100:
            raise DispatchError(
                "GENERATION_DISPATCH_INVALID_ENVELOPE", "Execution envelope is too deeply nested."
            )
        if isinstance(nested, dict):
            for key, child in nested.items():
                if not isinstance(key, str):
                    raise DispatchError(
                        "GENERATION_DISPATCH_INVALID_ENVELOPE",
                        "Execution envelope keys must be strings.",
                    )
                if FORBIDDEN.search(key):
                    raise DispatchError(
                        "GENERATION_DISPATCH_SENSITIVE_ENVELOPE",
                        "Execution envelope must not persist credential fields.",
                    )
                verify(child, depth + 1)
        elif isinstance(nested, list):
            for child in nested:
                verify(child, depth + 1)

    verify(value)
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError) as error:
        raise DispatchError(
            "GENERATION_DISPATCH_INVALID_ENVELOPE", "Execution envelope must be JSON serializable."
        ) from error
    maximum = configured("PI_AGENT_DISPATCH_ENVELOPE_MAX_BYTES", 262144, 1024, 4194304)
    if len(serialized.encode()) > maximum:
        raise DispatchError(
            "GENERATION_DISPATCH_ENVELOPE_TOO_LARGE",
            "Execution envelope exceeds its durable dispatch limit.",
        )
    return json.loads(serialized)


async def lock_job(connection, project, request):
    row = await one(
        connection,
        "SELECT * FROM agent_generation_jobs WHERE project_id=%s AND request_id=%s FOR UPDATE",
        (project, request),
    )
    if not row:
        raise DispatchError(
            "GENERATION_DISPATCH_NOT_FOUND",
            "Generation dispatch job does not exist.",
            project,
            request,
        )
    return row


def assert_fence(row, fence, now):
    if (
        str(row["id"]) != str(fence["jobId"])
        or row["project_id"] != fence["projectId"]
        or row["request_id"] != fence["requestId"]
        or row["status"] != "running"
        or row["lease_owner"] != fence["leaseOwner"]
        or row["fencing_token"] != fence["fencingToken"]
    ):
        raise DispatchError(
            "GENERATION_DISPATCH_LEASE_LOST",
            "The worker no longer owns the generation dispatch fence.",
            fence["projectId"],
            fence["requestId"],
        )
    if not row["lease_expires_at"] or utc(row["lease_expires_at"]) <= now:
        raise DispatchError(
            "GENERATION_DISPATCH_LEASE_EXPIRED",
            "The generation dispatch lease expired before its durable write.",
            fence["projectId"],
            fence["requestId"],
        )


async def event(connection, row, kind, payload, now, *, sequence=None):
    await connection.execute(
        """INSERT INTO agent_generation_outbox_events (id,job_id,project_id,request_id,sequence,event_type,payload,occurred_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            str(uuid4()),
            row["id"],
            row["project_id"],
            row["request_id"],
            sequence if sequence is not None else row["event_sequence"] + 1,
            kind,
            Jsonb(payload),
            now,
        ),
    )


class Dispatch:
    def __init__(self, database):
        self.database = database

    async def enqueue(
        self,
        project,
        request,
        instruction,
        *,
        execution_envelope=None,
        model=None,
        cli=None,
        max_attempts=1,
        stage="agent_execution",
    ):
        identifier(project)
        identifier(request)
        integer(max_attempts, 1, 10)
        if not isinstance(instruction, str) or stage not in {
            "planning_data_prefetch",
            "agent_execution",
        }:
            raise DispatchError(
                "GENERATION_DISPATCH_INVALID", "Invalid dispatch instruction or stage."
            )
        payload = envelope(
            execution_envelope
            if execution_envelope is not None
            else {"schemaVersion": 1, "instruction": instruction}
        )
        digest = "sha256:" + hashlib.sha256(instruction.encode()).hexdigest()
        preview = re.sub(r"\s+", " ", instruction).strip()
        preview = js_slice(preview, 0, 179) + "…" if js_length(preview) > 180 else preview
        async with self.database.transaction() as connection:
            if not await one(
                connection, "SELECT id FROM projects WHERE id=%s FOR UPDATE", (project,)
            ):
                raise DispatchError(
                    "GENERATION_DISPATCH_PROJECT_NOT_FOUND",
                    "Generation project does not exist.",
                    project,
                )
            existing = await one(
                connection,
                "SELECT * FROM agent_generation_jobs WHERE project_id=%s AND request_id=%s FOR UPDATE",
                (project, request),
            )
            if existing:
                if existing["instruction_hash"] != digest:
                    raise DispatchError(
                        "GENERATION_DISPATCH_IDEMPOTENCY_CONFLICT",
                        "The request ID is already bound to a different instruction.",
                        project,
                        request,
                    )
                return existing
            owner = await one(
                connection,
                "SELECT status,error_message FROM user_requests WHERE id=%s AND project_id=%s",
                (request, project),
            )
            if not owner:
                raise DispatchError(
                    "GENERATION_DISPATCH_REQUEST_NOT_FOUND",
                    "Generation request does not exist or belongs to another project.",
                    project,
                    request,
                )
            now = await database_now(connection)
            cancelled = owner["status"] == "cancelled"
            row = await one(
                connection,
                """INSERT INTO agent_generation_jobs (id,project_id,request_id,execution_envelope,instruction_hash,instruction_preview,
                cli_preference,selected_model,max_attempts,stage,available_at,queued_at,status,error_code,error_message,completed_at,event_sequence,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s) RETURNING *""",
                (
                    str(uuid4()),
                    project,
                    request,
                    Jsonb(payload),
                    digest,
                    preview,
                    cli,
                    model,
                    max_attempts,
                    stage,
                    now,
                    now,
                    "cancelled" if cancelled else "pending",
                    "USER_CANCELLED" if cancelled else None,
                    (owner["error_message"] or "用户暂停了当前任务") if cancelled else None,
                    now if cancelled else None,
                    now,
                ),
            )
            await event(
                connection,
                row,
                "generation_cancelled" if cancelled else "generation_queued",
                {"status": row["status"], "stage": stage, "maxAttempts": max_attempts},
                now,
                sequence=1,
            )
            return row

    async def claim(self, project, request, owner, lease_ttl_ms):
        for value in (project, request, owner):
            identifier(value)
        integer(lease_ttl_ms, 1, 86400000)
        async with self.database.transaction() as connection:
            if not await one(
                connection, "SELECT id FROM projects WHERE id=%s FOR UPDATE", (project,)
            ):
                raise DispatchError(
                    "GENERATION_DISPATCH_PROJECT_NOT_FOUND",
                    "Generation project does not exist.",
                    project,
                )
            row = await lock_job(connection, project, request)
            now = await database_now(connection)
            status = row["status"]
            if status in TERMINAL:
                raise DispatchError(
                    "GENERATION_DISPATCH_CANCELLED"
                    if status == "cancelled"
                    else "GENERATION_DISPATCH_TERMINAL",
                    f"Generation dispatch job is already {status}.",
                    project,
                    request,
                )
            if status == "running":
                expired = row["lease_expires_at"] and utc(row["lease_expires_at"]) <= now
                raise DispatchError(
                    "GENERATION_DISPATCH_REPLAN_REQUIRED"
                    if expired
                    else "GENERATION_DISPATCH_BUSY",
                    "The previous attempt must finish or be reconciled before claiming.",
                    project,
                    request,
                )
            if utc(row["available_at"]) > now:
                raise DispatchError(
                    "GENERATION_DISPATCH_NOT_AVAILABLE",
                    "Generation job is not available yet.",
                    project,
                    request,
                )
            if row["attempt_count"] >= row["max_attempts"]:
                raise DispatchError(
                    "GENERATION_DISPATCH_ATTEMPTS_EXHAUSTED",
                    "Generation attempt budget is exhausted.",
                    project,
                    request,
                )
            other = await one(
                connection,
                "SELECT request_id,lease_expires_at FROM agent_generation_jobs WHERE project_id=%s AND status='running' AND id<>%s LIMIT 1",
                (project, row["id"]),
            )
            if other:
                expired = other["lease_expires_at"] and utc(other["lease_expires_at"]) <= now
                raise DispatchError(
                    "GENERATION_PROJECT_REPLAN_REQUIRED" if expired else "GENERATION_PROJECT_BUSY",
                    "Another generation job is already running for this project.",
                    project,
                    other["request_id"],
                )
            request_row = await one(
                connection,
                "SELECT actor_user_id FROM user_requests WHERE id=%s AND project_id=%s",
                (request, project),
            )
            if not request_row:
                raise DispatchError(
                    "GENERATION_DISPATCH_REQUEST_NOT_FOUND",
                    "Generation request does not exist.",
                    project,
                    request,
                )
            actor = request_row["actor_user_id"]
            if actor:
                if not await one(
                    connection, "SELECT id FROM auth_users WHERE id=%s FOR UPDATE", (actor,)
                ):
                    raise DispatchError(
                        "GENERATION_DISPATCH_ACTOR_NOT_FOUND",
                        "Generation request actor no longer exists.",
                        project,
                        request,
                    )
                current = await one(
                    connection,
                    "SELECT count(*) AS n FROM agent_generation_jobs j JOIN user_requests r ON r.id=j.request_id WHERE j.status='running' AND j.id<>%s AND r.actor_user_id=%s",
                    (row["id"], actor),
                )
                await structural_capacity(
                    connection, actor, "agent.concurrent", current["n"], at=now
                )
            expiry = now + timedelta(milliseconds=lease_ttl_ms)
            changed = await one(
                connection,
                """UPDATE agent_generation_jobs SET status='running',lease_owner=%s,lease_expires_at=%s,last_heartbeat_at=%s,fencing_token=fencing_token+1,
                version=version+1,event_sequence=event_sequence+1,attempt_count=attempt_count+1,started_at=COALESCE(started_at,%s),completed_at=NULL,error_code=NULL,error_message=NULL,updated_at=%s
                WHERE id=%s AND version=%s AND status IN ('pending','retry_wait') RETURNING *""",
                (owner, expiry, now, now, now, row["id"], row["version"]),
            )
            if not changed:
                raise DispatchError(
                    "GENERATION_DISPATCH_CONFLICT",
                    "Dispatch claim lost a concurrent write.",
                    project,
                    request,
                )
            await event(
                connection,
                row,
                "generation_claimed",
                {
                    "status": "running",
                    "attemptCount": changed["attempt_count"],
                    "fencingToken": changed["fencing_token"],
                },
                now,
            )
            return {
                "jobId": str(row["id"]),
                "projectId": project,
                "requestId": request,
                "leaseOwner": owner,
                "fencingToken": changed["fencing_token"],
                "attemptCount": changed["attempt_count"],
                "leaseExpiresAt": iso(expiry),
            }

    async def heartbeat(self, fence, lease_ttl_ms):
        integer(lease_ttl_ms, 1, 86400000)
        async with self.database.transaction() as connection:
            row = await lock_job(connection, fence["projectId"], fence["requestId"])
            now = await database_now(connection)
            assert_fence(row, fence, now)
            expiry = now + timedelta(milliseconds=lease_ttl_ms)
            await connection.execute(
                "UPDATE agent_generation_jobs SET lease_expires_at=%s,last_heartbeat_at=%s,version=version+1,updated_at=%s WHERE id=%s",
                (expiry, now, now, row["id"]),
            )
            return {"leaseExpiresAt": iso(expiry)}

    async def read_claimed(self, fence):
        async with self.database.transaction() as connection:
            row = await lock_job(connection, fence["projectId"], fence["requestId"])
            if row["status"] == "cancelled":
                raise DispatchError(
                    "GENERATION_DISPATCH_CANCELLED", "Generation job was cancelled."
                )
            assert_fence(row, fence, await database_now(connection))
            return row

    async def checkpoint(self, fence, execution_envelope):
        payload = envelope(execution_envelope)
        async with self.database.transaction() as connection:
            row = await lock_job(connection, fence["projectId"], fence["requestId"])
            now = await database_now(connection)
            assert_fence(row, fence, now)
            if row["stage"] != "planning_data_prefetch":
                raise DispatchError(
                    "GENERATION_DISPATCH_STAGE_CONFLICT", "Preparation was already checkpointed."
                )
            await connection.execute(
                "UPDATE agent_generation_jobs SET execution_envelope=%s,stage='agent_execution',max_attempts=3,version=version+1,event_sequence=event_sequence+1,updated_at=%s WHERE id=%s",
                (Jsonb(payload), now, row["id"]),
            )
            await event(
                connection,
                row,
                "generation_preparation_completed",
                {
                    "status": "running",
                    "stage": "agent_execution",
                    "recoveryMode": "prepared_inputs",
                },
                now,
            )

    async def finish(self, project, request, status, *, fence=None, code=None, message=None):
        if status not in TERMINAL - {"cancelled"}:
            raise DispatchError("GENERATION_DISPATCH_INVALID", "Unsupported terminal status.")
        async with self.database.transaction() as connection:
            row = await lock_job(connection, project, request)
            now = await database_now(connection)
            if row["status"] == "cancelled" or row["status"] == status:
                return row
            if row["status"] in TERMINAL:
                raise DispatchError(
                    "GENERATION_DISPATCH_TERMINAL_CONFLICT",
                    "Generation job already has a different terminal status.",
                    project,
                    request,
                )
            if not fence:
                raise DispatchError(
                    "GENERATION_DISPATCH_FENCE_REQUIRED",
                    "A live dispatch fence is required for completion.",
                    project,
                    request,
                )
            assert_fence(row, fence, now)
            changed = await one(
                connection,
                """UPDATE agent_generation_jobs SET status=%s,stage=CASE WHEN %s='completed' THEN 'completed' ELSE stage END,
                lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL,version=version+1,event_sequence=event_sequence+1,error_code=%s,error_message=%s,completed_at=%s,updated_at=%s
                WHERE id=%s RETURNING *""",
                (status, status, code, message, now, now, row["id"]),
            )
            await event(
                connection,
                row,
                "generation_" + status,
                {"status": status, "errorCode": code, "errorMessage": message},
                now,
            )
            if status in {"failed", "interrupted"}:
                await connection.execute(
                    "UPDATE user_requests SET status='failed',completed_at=%s,error_message=%s WHERE id=%s AND project_id=%s AND status IN ('pending','processing')",
                    (now, message or code or "Generation failed.", request, project),
                )
            return changed

    async def cancel(self, project, request, reason=None):
        async with self.database.transaction() as connection:
            try:
                row = await lock_job(connection, project, request)
            except DispatchError as error:
                if error.code == "GENERATION_DISPATCH_NOT_FOUND":
                    return None
                raise
            if row["status"] in TERMINAL:
                return row
            now = await database_now(connection)
            reason = reason if reason is not None else "用户暂停了当前任务"
            changed = await one(
                connection,
                """UPDATE agent_generation_jobs SET status='cancelled',lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL,
                fencing_token=fencing_token+1,version=version+1,event_sequence=event_sequence+1,error_code='USER_CANCELLED',error_message=%s,completed_at=%s,updated_at=%s WHERE id=%s RETURNING *""",
                (reason, now, now, row["id"]),
            )
            await event(
                connection,
                row,
                "generation_cancelled",
                {"status": "cancelled", "reason": reason},
                now,
            )
            return changed

    async def reconcile(self, *, project=None, limit=100):
        integer(limit, 1, 1000)
        grace = configured("PI_AGENT_DISPATCH_PENDING_ORPHAN_GRACE_MS", 120000, 1000, 86400000)
        candidates = await self.database.fetch_all(
            """SELECT project_id,request_id FROM agent_generation_jobs
            WHERE ((status='running' AND lease_expires_at<=clock_timestamp()) OR (status IN ('pending','retry_wait') AND available_at<=clock_timestamp()-(%s*interval '1 millisecond')))
            AND (%s::text IS NULL OR project_id=%s) ORDER BY COALESCE(lease_expires_at,available_at) LIMIT %s""",
            (grace, project, project, limit),
        )
        results = []
        for candidate in candidates:
            result = await self._reconcile(candidate, grace)
            if result:
                results.append(result)
        return results

    async def _reconcile(self, candidate, grace):
        async with self.database.transaction() as connection:
            row = await one(
                connection,
                "SELECT * FROM agent_generation_jobs WHERE project_id=%s AND request_id=%s FOR UPDATE",
                (candidate["project_id"], candidate["request_id"]),
            )
            if not row:
                return None
            now = await database_now(connection)
            expired = (
                row["status"] == "running"
                and row["lease_expires_at"]
                and utc(row["lease_expires_at"]) <= now
            )
            orphan = row["status"] in {"pending", "retry_wait"} and utc(
                row["available_at"]
            ) <= now - timedelta(milliseconds=grace)
            if not expired and not orphan:
                return None
            binding = (row["project_id"], row["request_id"], now)
            live_lease = await one(
                connection,
                "SELECT project_id FROM agent_generation_leases WHERE project_id=%s AND active_request_id=%s AND status='held' AND lease_expires_at>%s",
                binding,
            )
            live_run = await one(
                connection,
                "SELECT id FROM agent_runs WHERE project_id=%s AND request_id=%s AND status IN ('pending','running','reconciling','waiting') AND lease_expires_at>%s LIMIT 1",
                binding,
            )
            mission = await one(
                connection,
                "SELECT * FROM agent_missions WHERE project_id=%s AND request_id=%s FOR UPDATE",
                binding[:2],
            )
            if (
                live_lease
                or live_run
                or (
                    mission
                    and mission["status"] == "verifying"
                    and mission["verification_lease_expires_at"]
                    and utc(mission["verification_lease_expires_at"]) > now
                )
            ):
                return None
            if (
                os.getenv("PI_AGENT_DISPATCH_MODE") == "worker"
                and row["attempt_count"] < row["max_attempts"]
            ):
                base = configured("PI_AGENT_DISPATCH_RETRY_BASE_DELAY_MS", 5000, 100, 3600000)
                available = now + timedelta(
                    milliseconds=min(3600000, base * 2 ** max(0, row["attempt_count"] - 1))
                )
                code = (
                    "DISPATCH_LEASE_EXPIRED_RETRY_SCHEDULED"
                    if expired
                    else "DISPATCH_PENDING_ORPHAN_RETRY_SCHEDULED"
                )
                changed = await one(
                    connection,
                    """UPDATE agent_generation_jobs SET status='retry_wait',available_at=%s,lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL,
                    fencing_token=fencing_token+1,version=version+1,event_sequence=event_sequence+1,error_code=%s,error_message=%s,completed_at=NULL,updated_at=%s WHERE id=%s RETURNING *""",
                    (
                        available,
                        code,
                        "Generation attempt lost its worker; a new attempt has been scheduled.",
                        now,
                        row["id"],
                    ),
                )
                await event(
                    connection,
                    row,
                    "generation_retry_scheduled",
                    {
                        "status": "retry_wait",
                        "attemptCount": row["attempt_count"],
                        "maxAttempts": row["max_attempts"],
                        "availableAt": iso(available),
                        "recoveryMode": "replan_required",
                    },
                    now,
                )
                return changed
            code = (
                "DISPATCH_LEASE_EXPIRED_REPLAN_REQUIRED"
                if expired
                else "DISPATCH_PENDING_ORPHAN_REPLAN_REQUIRED"
            )
            changed = await one(
                connection,
                """UPDATE agent_generation_jobs SET status='interrupted',lease_owner=NULL,lease_expires_at=NULL,last_heartbeat_at=NULL,
                fencing_token=fencing_token+1,version=version+1,event_sequence=event_sequence+1,error_code=%s,error_message=%s,completed_at=%s,updated_at=%s WHERE id=%s RETURNING *""",
                (
                    code,
                    "Generation worker lease expired; a new request must replan from durable state."
                    if expired
                    else "Generation job was persisted but never claimed; a new request must replan from durable state.",
                    now,
                    now,
                    row["id"],
                ),
            )
            await event(
                connection,
                row,
                "generation_interrupted",
                {"status": "interrupted", "code": code, "recoveryMode": "replan_required"},
                now,
            )
            await connection.execute(
                "UPDATE user_requests SET status='failed',error_message=%s,completed_at=%s WHERE id=%s AND project_id=%s AND status IN ('pending','processing','active','running')",
                (
                    "生成 worker 中断；旧 attempt 已封存，请重新发起任务以基于持久化状态重规划。",
                    now,
                    row["request_id"],
                    row["project_id"],
                ),
            )
            if mission and mission["status"] not in {"completed", "failed", "cancelled"}:
                result = await connection.execute(
                    """UPDATE agent_missions SET status='failed',active_slot=NULL,version=version+1,error_code=%s,error_message=%s,
                    verification_lease_owner=NULL,verification_lease_expires_at=NULL,verification_last_heartbeat_at=NULL,completed_at=%s,updated_at=%s WHERE id=%s AND version=%s""",
                    (
                        code,
                        "Generation worker lease expired; the old Mission was closed before replanning.",
                        now,
                        now,
                        mission["id"],
                        mission["version"],
                    ),
                )
                if result.rowcount != 1:
                    raise DispatchError(
                        "GENERATION_DISPATCH_RECONCILIATION_CONFLICT",
                        "Mission changed during dispatch reconciliation.",
                    )
                await connection.execute(
                    "UPDATE agent_mission_nodes SET status='failed',version=version+1,finished_at=%s,updated_at=%s WHERE mission_id=%s AND status IN ('pending','running')",
                    (now, now, mission["id"]),
                )
            return changed

    async def claimable(self, limit=20):
        integer(limit, 1, 200)
        return await self.database.fetch_all(
            """WITH ranked AS (SELECT j.*,ROW_NUMBER() OVER
            (PARTITION BY COALESCE(r.actor_user_id,'workspace:'||j.project_id) ORDER BY j.available_at,j.queued_at,j.id) AS actor_rank
            FROM agent_generation_jobs j JOIN user_requests r ON r.id=j.request_id AND r.project_id=j.project_id
            WHERE j.status IN ('pending','retry_wait') AND j.available_at<=clock_timestamp()
            AND NOT EXISTS (SELECT 1 FROM agent_generation_leases l WHERE l.project_id=j.project_id AND l.status='held' AND l.lease_expires_at>clock_timestamp()))
            SELECT * FROM ranked ORDER BY actor_rank,available_at,queued_at,id LIMIT %s""",
            (limit,),
        )

    async def jobs(self, project, limit=50):
        identifier(project)
        return await self.database.fetch_all(
            "SELECT * FROM agent_generation_jobs WHERE project_id=%s ORDER BY queued_at DESC,id DESC LIMIT %s",
            (project, max(1, min(limit, 200))),
        )

    async def get(self, project, request):
        return await self.database.fetch_one(
            "SELECT * FROM agent_generation_jobs WHERE project_id=%s AND request_id=%s",
            (project, request),
        )

    async def pending_events(self, project, limit=200):
        return await self.database.fetch_all(
            "SELECT * FROM agent_generation_outbox_events WHERE project_id=%s AND published_at IS NULL ORDER BY created_at,sequence LIMIT %s",
            (project, max(1, min(limit, 1000))),
        )

    async def mark_published(self, identities):
        if not identities:
            return 0
        async with self.database.transaction() as connection:
            result = await connection.execute(
                "UPDATE agent_generation_outbox_events SET published_at=now() WHERE id=ANY(%s::uuid[]) AND published_at IS NULL",
                (list(identities),),
            )
            return result.rowcount
