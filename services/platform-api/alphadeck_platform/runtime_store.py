import re
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .generation_lease import database_now
from .quota import one, utc
from .runtime_policy import (
    ACTIVE,
    BOUNDARIES,
    EFFECTS,
    IDEMPOTENCY,
    TERMINAL,
    RuntimeStoreError,
    identifier,
    integer,
    public_json,
    usage,
)


def snake(name):
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def fence_for(row):
    return {
        "runId": row["id"],
        "expectedVersion": row["version"],
        "leaseOwner": row["lease_owner"],
        "fencingToken": row["fencing_token"],
        "workspaceFencingToken": row["workspace_fencing_token"],
    }


async def update(connection, table, identity, values, *, version=False, key="id"):
    columns = list(values)
    parts = [sql.SQL("{}=%s").format(sql.Identifier(column)) for column in columns]
    if version:
        parts.append(sql.SQL("version=version+1"))
    parts.append(sql.SQL("updated_at=clock_timestamp()"))
    query = sql.SQL("UPDATE {} SET {} WHERE {}=%s RETURNING *").format(
        sql.Identifier(table), sql.SQL(",").join(parts), sql.Identifier(key)
    )
    return await one(connection, query, tuple(values[column] for column in columns) + (identity,))


async def insert(connection, table, values):
    columns = list(values)
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
        sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns),
    )
    return await one(connection, query, tuple(values[column] for column in columns))


async def unresolved(connection, project, *, run=None, exclude=None):
    return await one(
        connection,
        """SELECT e.* FROM agent_tool_executions e JOIN agent_runs r ON r.id=e.run_id WHERE r.project_id=%s
        AND e.status IN ('prepared','commit_authorized','uncertain') AND e.effect IN ('workspace_write','external_write')
        AND (%s::text IS NULL OR e.run_id=%s) AND (%s::text IS NULL OR e.operation_id<>%s) LIMIT 1""",
        (project, run, run, exclude, exclude),
    )


async def acquire_workspace(connection, project, workspace, run, owner, ttl, *, block=True):
    await connection.execute(
        "INSERT INTO agent_workspace_leases (project_id,workspace_key,updated_at) VALUES (%s,%s,now()) ON CONFLICT DO NOTHING",
        (project, workspace),
    )
    current = await one(
        connection,
        "SELECT * FROM agent_workspace_leases WHERE project_id=%s FOR UPDATE",
        (project,),
    )
    if not current or current["workspace_key"] != workspace:
        raise RuntimeStoreError(
            "WORKSPACE_BINDING_CONFLICT",
            "Canonical workspace is already bound to another project or path.",
        )
    now = await database_now(connection)
    if (
        current["status"] == "held"
        and current["lease_owner"]
        and current["lease_expires_at"]
        and utc(current["lease_expires_at"]) > now
    ):
        raise RuntimeStoreError("WORKSPACE_BUSY", "Workspace lease is still active.")
    if block and await unresolved(connection, project):
        raise RuntimeStoreError(
            "RECONCILIATION_REQUIRED", "Workspace has unresolved mutating operations."
        )
    if current["active_run_id"] and current["active_run_id"] != run:
        await connection.execute(
            """UPDATE agent_runs SET status='interrupted',error_code='WORKSPACE_LEASE_EXPIRED_REPLAN_REQUIRED',
            error_message='Expired workspace owner was fenced by a new replan run.',finished_at=%s,lease_owner=NULL,lease_expires_at=NULL,version=version+1,updated_at=%s
            WHERE id=%s AND status IN ('pending','running','reconciling','waiting')""",
            (now, now, current["active_run_id"]),
        )
    return await update(
        connection,
        "agent_workspace_leases",
        project,
        {
            "status": "held",
            "active_run_id": run,
            "lease_owner": owner,
            "lease_expires_at": now + timedelta(milliseconds=ttl),
            "last_heartbeat_at": now,
            "fencing_token": current["fencing_token"] + 1,
            "acquired_at": now,
            "released_at": None,
        },
        version=True,
        key="project_id",
    )


def check_fence(run, lease, fence, now, *, replay=False):
    if not run:
        raise RuntimeStoreError("NOT_FOUND", "Agent run was not found.")
    expected = fence["expectedVersion"]
    if expected > run["version"] if replay else expected != run["version"]:
        raise RuntimeStoreError("CONFLICT", "Agent run version changed.")
    if (
        run["lease_owner"] != fence["leaseOwner"]
        or run["fencing_token"] != fence["fencingToken"]
        or not run["lease_expires_at"]
        or utc(run["lease_expires_at"]) <= now
        or not lease
        or lease["status"] != "held"
        or lease["workspace_key"] != run["workspace_key"]
        or lease["active_run_id"] != run["id"]
        or lease["lease_owner"] != fence["leaseOwner"]
        or lease["fencing_token"] != fence["workspaceFencingToken"]
        or run["workspace_fencing_token"] != fence["workspaceFencingToken"]
        or not lease["lease_expires_at"]
        or utc(lease["lease_expires_at"]) <= now
    ):
        raise RuntimeStoreError("LEASE_LOST", "Agent run or workspace lease was lost.")
    if run["status"] not in ACTIVE:
        raise RuntimeStoreError("INVALID_STATE", "Agent run is terminal.")


class RuntimeStore:
    def __init__(self, database):
        self.database = database

    @asynccontextmanager
    async def locked(self, fence, *, replay=False, validate=True):
        async with self.database.transaction() as connection:
            binding = await one(
                connection, "SELECT project_id FROM agent_runs WHERE id=%s", (fence["runId"],)
            )
            if not binding:
                raise RuntimeStoreError("NOT_FOUND", "Agent run was not found.")
            lease = await one(
                connection,
                "SELECT * FROM agent_workspace_leases WHERE project_id=%s FOR UPDATE",
                (binding["project_id"],),
            )
            run = await one(
                connection, "SELECT * FROM agent_runs WHERE id=%s FOR UPDATE", (fence["runId"],)
            )
            now = await database_now(connection)
            if validate:
                check_fence(run, lease, fence, now, replay=replay)
            yield connection, run, lease, now

    async def create(self, input, *, owner=None, lease_ttl_ms=60000):
        required = (
            "id",
            "projectId",
            "workspaceKey",
            "provider",
            "model",
            "frameworkVersion",
            "buildRevision",
            "profileHash",
            "promptHash",
            "toolHash",
            "skillHash",
            "workspaceHash",
        )
        for key in required:
            identifier(
                input.get(key), 512 if key.endswith("Hash") or key == "workspaceKey" else 256
            )
        if input.get("requestId") is not None:
            identifier(input["requestId"])
        if owner:
            identifier(owner)
            integer(lease_ttl_ms, positive=True)
        if input.get("status", "pending") not in ACTIVE:
            raise RuntimeStoreError("INVALID_STATE", "Initial run status must be active.")
        instance = input.get("runInstanceId", str(uuid4()))
        try:
            parsed = UUID(instance)
            if parsed.variant != "specified in RFC 4122" or not 1 <= parsed.version <= 8:
                raise ValueError("UUID version")
        except (ValueError, TypeError, AttributeError) as error:
            raise RuntimeStoreError("INVALID_STATE", "runInstanceId must be a UUID.") from error
        try:
            async with self.database.transaction() as connection:
                request = (
                    await one(
                        connection,
                        "SELECT project_id,actor_user_id FROM user_requests WHERE id=%s",
                        (input["requestId"],),
                    )
                    if input.get("requestId")
                    else None
                )
                if input.get("requestId") and (
                    not request or request["project_id"] != input["projectId"]
                ):
                    raise RuntimeStoreError(
                        "INVALID_STATE", "Agent request is not bound to this project."
                    )
                lease = (
                    await acquire_workspace(
                        connection,
                        input["projectId"],
                        input["workspaceKey"],
                        input["id"],
                        owner,
                        lease_ttl_ms,
                    )
                    if owner
                    else None
                )
                now = await database_now(connection)
                data = {snake(key): input[key] for key in required}
                data.update(
                    run_instance_id=instance,
                    request_id=input.get("requestId"),
                    actor_user_id=request["actor_user_id"] if request else None,
                    status=input.get("status", "running" if owner else "pending"),
                    lease_owner=owner,
                    lease_expires_at=lease["lease_expires_at"] if lease else None,
                    last_heartbeat_at=lease["last_heartbeat_at"] if lease else None,
                    fencing_token=1 if owner else 0,
                    workspace_fencing_token=lease["fencing_token"] if lease else 0,
                    started_at=input.get(
                        "startedAt", lease["last_heartbeat_at"] if lease else None
                    ),
                    updated_at=now,
                )
                return await insert(connection, "agent_runs", data)
        except UniqueViolation as error:
            raise RuntimeStoreError("CONFLICT", "Agent run already exists.") from error

    async def get(self, run):
        return await self.database.fetch_one("SELECT * FROM agent_runs WHERE id=%s", (run,))

    async def claim(
        self,
        run,
        owner,
        expected_version,
        lease_ttl_ms=60000,
        *,
        allow_terminal_reconciliation=False,
    ):
        identifier(owner)
        integer(expected_version)
        integer(lease_ttl_ms, positive=True)
        async with self.database.transaction() as connection:
            current = await one(connection, "SELECT * FROM agent_runs WHERE id=%s", (run,))
            if not current:
                raise RuntimeStoreError("NOT_FOUND", "Agent run was not found.")
            if current["version"] != expected_version:
                raise RuntimeStoreError("CONFLICT", "Agent run version changed.")
            terminal = current["status"] not in ACTIVE
            if terminal and not (
                allow_terminal_reconciliation
                and await unresolved(connection, current["project_id"], run=run)
            ):
                raise RuntimeStoreError("INVALID_STATE", "Terminal run cannot be leased.")
            lease = await acquire_workspace(
                connection,
                current["project_id"],
                current["workspace_key"],
                run,
                owner,
                lease_ttl_ms,
                block=False,
            )
            now = await database_now(connection)
            current = await one(
                connection, "SELECT * FROM agent_runs WHERE id=%s FOR UPDATE", (run,)
            )
            if current["version"] != expected_version:
                raise RuntimeStoreError("CONFLICT", "Agent run version changed.")
            if current["lease_expires_at"] and utc(current["lease_expires_at"]) > now:
                raise RuntimeStoreError("LEASE_LOST", "Agent run lease is still active.")
            changes = {
                "lease_owner": owner,
                "lease_expires_at": lease["lease_expires_at"],
                "last_heartbeat_at": lease["last_heartbeat_at"],
                "fencing_token": current["fencing_token"] + 1,
                "workspace_fencing_token": lease["fencing_token"],
                "status": "running" if current["status"] == "pending" else "reconciling",
                "started_at": current["started_at"] or now,
            }
            if terminal:
                changes.update(finished_at=None, error_code=None, error_message=None)
            return await update(connection, "agent_runs", run, changes, version=True)

    async def heartbeat(self, fence, lease_ttl_ms=60000):
        integer(lease_ttl_ms, positive=True)
        async with self.locked(fence) as (connection, run, lease, now):
            expiry = now + timedelta(milliseconds=lease_ttl_ms)
            await update(
                connection,
                "agent_workspace_leases",
                run["project_id"],
                {"lease_expires_at": expiry, "last_heartbeat_at": now},
                version=True,
                key="project_id",
            )
            return await update(
                connection,
                "agent_runs",
                run["id"],
                {"lease_expires_at": expiry, "last_heartbeat_at": now},
                version=True,
            )

    async def append(
        self,
        fence,
        event_id,
        sequence,
        event_type,
        payload,
        occurred_at,
        *,
        cumulative_usage=None,
        transition=None,
    ):
        identifier(event_id, 512)
        identifier(event_type)
        integer(sequence, positive=True)
        payload = public_json(payload)
        if transition is not None and transition not in {"running", "waiting"}:
            raise RuntimeStoreError(
                "INVALID_STATE", "Event transition must target running or waiting."
            )
        counters = usage(cumulative_usage) if cumulative_usage is not None else None
        async with self.locked(fence, validate=False) as (connection, run, lease, now):
            existing = await one(
                connection,
                "SELECT * FROM agent_events WHERE event_id=%s OR (run_id=%s AND sequence=%s)",
                (event_id, run["id"], sequence),
            )
            if existing:
                if (
                    existing["run_id"],
                    existing["event_id"],
                    existing["sequence"],
                    existing["event_type"],
                    utc(existing["occurred_at"]),
                    existing["payload"],
                ) != (run["id"], event_id, sequence, event_type, utc(occurred_at), payload):
                    raise RuntimeStoreError(
                        "CONFLICT", "Event ID or sequence is already bound to different content."
                    )
                check_fence(run, lease, fence, now, replay=True)
                return {"run": run, "event": existing}
            check_fence(run, lease, fence, now)
            if run["last_event_sequence"] >= sequence or (
                transition
                and run["status"] != ("running" if transition == "waiting" else "waiting")
            ):
                raise RuntimeStoreError("CONFLICT", "Event sequence or run transition changed.")
            changes = {"last_event_sequence": sequence}
            if transition:
                changes["status"] = transition
            if counters:
                changes.update({snake(key): value for key, value in counters.items()})
            changed = await update(connection, "agent_runs", run["id"], changes, version=True)
            record = await insert(
                connection,
                "agent_events",
                {
                    "id": str(uuid4()),
                    "event_id": event_id,
                    "run_id": run["id"],
                    "sequence": sequence,
                    "event_type": event_type,
                    "payload": Jsonb(payload),
                    "occurred_at": occurred_at,
                },
            )
            return {"run": changed, "event": record}

    async def checkpoint(
        self,
        fence,
        sequence,
        turn,
        boundary,
        public_state,
        state_hash,
        *,
        state_version=1,
        opaque=None,
    ):
        integer(sequence, positive=True)
        integer(turn)
        integer(state_version, positive=True)
        identifier(state_hash, 512)
        if boundary not in BOUNDARIES:
            raise RuntimeStoreError("INVALID_STATE", "Unknown checkpoint boundary.")
        if opaque and (
            opaque.get("codec") not in {"reference-v1", "sealed-v1"}
            or not isinstance(opaque.get("value"), str)
            or not opaque["value"]
            or len(opaque["value"].encode()) > 524288
            or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", opaque["value"])
        ):
            raise RuntimeStoreError("INVALID_STATE", "Invalid checkpoint opaque envelope.")
        state = public_json(public_state)
        async with self.locked(fence) as (connection, run, _, _now):
            if (
                run["last_event_sequence"] < sequence
                or run["turn_count"] > turn
                or (
                    run["latest_checkpoint_sequence"] is not None
                    and run["latest_checkpoint_sequence"] >= sequence
                )
            ):
                raise RuntimeStoreError(
                    "CONFLICT", "Checkpoint must advance at a committed event boundary."
                )
            changed = await update(
                connection,
                "agent_runs",
                run["id"],
                {"latest_checkpoint_sequence": sequence, "turn_count": turn},
                version=True,
            )
            record = await insert(
                connection,
                "agent_checkpoints",
                {
                    "id": str(uuid4()),
                    "run_id": run["id"],
                    "sequence": sequence,
                    "turn": turn,
                    "boundary": boundary,
                    "recovery_mode": "replan_required",
                    "public_state": Jsonb(state),
                    "opaque_state": opaque["value"] if opaque else None,
                    "opaque_codec": opaque["codec"] if opaque else None,
                    "state_hash": state_hash,
                    "state_version": state_version,
                    "fencing_token": fence["fencingToken"],
                },
            )
            return {"run": changed, "checkpoint": record}

    async def prepare_tool(
        self,
        fence,
        operation,
        call_id,
        tool,
        input_hash,
        effect,
        idempotency,
        *,
        idempotency_key=None,
        pre_state_hash=None,
    ):
        for value, maximum in ((operation, 512), (call_id, 512), (tool, 256), (input_hash, 512)):
            identifier(value, maximum)
        if pre_state_hash is not None:
            identifier(pre_state_hash, 512)
        if idempotency_key is not None:
            identifier(idempotency_key, 512)
        if (
            effect not in EFFECTS
            or idempotency not in IDEMPOTENCY
            or (idempotency == "operation_key" and not idempotency_key)
        ):
            raise RuntimeStoreError("INVALID_STATE", "Invalid tool effect or idempotency policy.")
        async with self.locked(fence, validate=False) as (connection, run, lease, now):
            existing = await one(
                connection,
                "SELECT * FROM agent_tool_executions WHERE operation_id=%s FOR UPDATE",
                (operation,),
            )
            if existing:
                if (
                    existing["run_id"],
                    existing["tool_call_id"],
                    existing["tool_name"],
                    existing["input_hash"],
                    existing["effect"],
                    existing["idempotency"],
                    existing["idempotency_key"],
                ) != (
                    run["id"],
                    call_id,
                    tool,
                    input_hash,
                    effect,
                    idempotency,
                    idempotency_key,
                ) or (pre_state_hash is not None and existing["pre_state_hash"] != pre_state_hash):
                    raise RuntimeStoreError(
                        "OPERATION_CONFLICT",
                        "Operation ID is already bound to a different tool execution.",
                    )
                check_fence(run, lease, fence, now, replay=True)
                return {"run": run, "execution": existing, "created": False}
            check_fence(run, lease, fence, now)
            if effect in {"workspace_write", "external_write"} and await unresolved(
                connection, run["project_id"], exclude=operation
            ):
                raise RuntimeStoreError(
                    "RECONCILIATION_REQUIRED", "Workspace has an unresolved mutating operation."
                )
            changed = await update(connection, "agent_runs", run["id"], {}, version=True)
            record = await insert(
                connection,
                "agent_tool_executions",
                {
                    "id": str(uuid4()),
                    "run_id": run["id"],
                    "operation_id": operation,
                    "tool_call_id": call_id,
                    "tool_name": tool,
                    "input_hash": input_hash,
                    "effect": effect,
                    "idempotency": idempotency,
                    "idempotency_key": idempotency_key,
                    "pre_state_hash": pre_state_hash,
                    "fencing_token": fence["fencingToken"],
                    "workspace_fencing_token": fence["workspaceFencingToken"],
                    "updated_at": now,
                },
            )
            return {"run": changed, "execution": record, "created": True}

    async def complete_tool(
        self,
        fence,
        operation,
        status,
        *,
        receipt=None,
        error=None,
        pre_state_hash=None,
        post_state_hash=None,
    ):
        if (
            status not in {"succeeded", "failed", "uncertain"}
            or (status == "succeeded" and error)
            or (status != "succeeded" and not error)
        ):
            raise RuntimeStoreError("INVALID_STATE", "Invalid tool completion or public error.")
        receipt = public_json(receipt) if receipt is not None else None
        for value in (pre_state_hash, post_state_hash):
            if value is not None:
                identifier(value, 512)
        async with self.locked(fence, validate=False) as (connection, run, lease, now):
            record = await one(
                connection,
                "SELECT * FROM agent_tool_executions WHERE operation_id=%s FOR UPDATE",
                (operation,),
            )
            if not record or record["run_id"] != run["id"]:
                raise RuntimeStoreError("NOT_FOUND", "Tool execution was not found.")
            if (
                pre_state_hash is not None
                and record["pre_state_hash"] is not None
                and record["pre_state_hash"] != pre_state_hash
            ):
                raise RuntimeStoreError(
                    "OPERATION_CONFLICT", "Tool pre-state hash cannot be overwritten."
                )
            replay = record["status"] not in {"prepared", "commit_authorized"}
            if replay:
                if (
                    record["status"],
                    record["post_state_hash"],
                    record["error_code"],
                    record["error_message"],
                    record["result_receipt"],
                ) != (
                    status,
                    post_state_hash,
                    (error or {}).get("code"),
                    (error or {}).get("message"),
                    receipt,
                ) or (pre_state_hash is not None and record["pre_state_hash"] != pre_state_hash):
                    raise RuntimeStoreError(
                        "OPERATION_CONFLICT",
                        "Tool operation already reached a different terminal state.",
                    )
                check_fence(run, lease, fence, now, replay=True)
                return {"run": run, "execution": record}
            check_fence(run, lease, fence, now)
            if (
                status == "succeeded"
                and record["effect"] == "workspace_write"
                and record["status"] != "commit_authorized"
            ):
                raise RuntimeStoreError(
                    "OPERATION_CONFLICT",
                    "Workspace mutation cannot succeed before physical commit authorization.",
                )
            changed = await update(connection, "agent_runs", run["id"], {}, version=True)
            record = await update(
                connection,
                "agent_tool_executions",
                record["id"],
                {
                    "status": status,
                    "result_receipt": Jsonb(receipt) if receipt is not None else None,
                    "pre_state_hash": pre_state_hash
                    if pre_state_hash is not None
                    else record["pre_state_hash"],
                    "post_state_hash": post_state_hash,
                    "error_code": (error or {}).get("code"),
                    "error_message": (error or {}).get("message"),
                    "completed_at": now,
                },
            )
            return {"run": changed, "execution": record}

    async def authorize_mutation(self, fence, operation, commit):
        async with self.locked(fence) as (connection, run, _lease, _now):
            if run["status"] != "running":
                raise RuntimeStoreError("CONFLICT", "Agent run is not running before commit.")
            record = await one(
                connection,
                "SELECT * FROM agent_tool_executions WHERE operation_id=%s FOR UPDATE",
                (operation,),
            )
            if (
                not record
                or record["run_id"] != run["id"]
                or record["status"] != "prepared"
                or record["effect"] != "workspace_write"
                or record["fencing_token"] != fence["fencingToken"]
                or record["workspace_fencing_token"] != fence["workspaceFencingToken"]
            ):
                raise RuntimeStoreError(
                    "OPERATION_CONFLICT",
                    "Workspace mutation requires its own prepared operation ledger.",
                )
            await update(
                connection, "agent_tool_executions", record["id"], {"status": "commit_authorized"}
            )
        # Caller holds the shared filesystem lock. Authorization commits before physical effects,
        # so an ambiguous crash remains unresolved and is never automatically replayed.
        return await commit()

    async def complete(self, fence, status, turn_count, counters, *, error=None, finished_at=None):
        integer(turn_count)
        counters = usage(counters)
        if status not in TERMINAL or (status == "failed" and not error):
            raise RuntimeStoreError(
                "INVALID_STATE",
                "Run completion requires a terminal status and public error for failure.",
            )
        async with self.locked(fence) as (connection, run, _lease, now):
            changed = await update(
                connection,
                "agent_runs",
                run["id"],
                {
                    "status": status,
                    "turn_count": turn_count,
                    **{snake(key): value for key, value in counters.items()},
                    "error_code": (error or {}).get("code"),
                    "error_message": (error or {}).get("message"),
                    "finished_at": finished_at or now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                },
                version=True,
            )
            await update(
                connection,
                "agent_workspace_leases",
                run["project_id"],
                {
                    "status": "free",
                    "active_run_id": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_heartbeat_at": now,
                    "released_at": now,
                },
                version=True,
                key="project_id",
            )
            return changed

    async def events(self, run, after=0, limit=500):
        integer(after)
        integer(limit, positive=True)
        return await self.database.fetch_all(
            "SELECT * FROM agent_events WHERE run_id=%s AND sequence>%s ORDER BY sequence LIMIT %s",
            (run, after, min(limit, 1000)),
        )

    async def latest_checkpoint(self, run):
        return await self.database.fetch_one(
            "SELECT * FROM agent_checkpoints WHERE run_id=%s ORDER BY sequence DESC LIMIT 1", (run,)
        )

    async def reconciliation_candidates(self, *, project=None, limit=100):
        integer(limit, positive=True)
        records = await self.database.fetch_all(
            """SELECT r.* FROM agent_runs r WHERE (%s::text IS NULL OR r.project_id=%s) AND
            ((r.status IN ('pending','running','reconciling','waiting') AND (r.lease_expires_at IS NULL OR r.lease_expires_at<=clock_timestamp())) OR
            EXISTS (SELECT 1 FROM agent_tool_executions e WHERE e.run_id=r.id AND e.status IN ('prepared','commit_authorized','uncertain') AND e.effect IN ('workspace_write','external_write')))
            ORDER BY r.created_at LIMIT %s""",
            (project, project, min(limit, 1000)),
        )
        result = []
        for record in records:
            tools = await self.database.fetch_all(
                "SELECT * FROM agent_tool_executions WHERE run_id=%s AND status IN ('prepared','commit_authorized','uncertain') AND effect IN ('workspace_write','external_write') ORDER BY prepared_at",
                (record["id"],),
            )
            result.append(
                {
                    "run": record,
                    "latestCheckpoint": await self.latest_checkpoint(record["id"]),
                    "unresolvedToolExecutions": tools,
                }
            )
        return result
