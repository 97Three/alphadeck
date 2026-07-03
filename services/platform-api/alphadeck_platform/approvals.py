import asyncio
import hashlib
import re
from contextlib import suppress
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from .quota import one, utc
from .runtime_policy import RuntimeStoreError, public_json
from .runtime_store import snake
from .sessions import iso

STATUSES = {"pending", "approved", "edited", "rejected", "expired"}
PRIORITY = {"pending": 0, "edited": 1, "approved": 2, "rejected": 3, "expired": 4}
FIELDS = (
    "id",
    "runId",
    "turn",
    "toolName",
    "effect",
    "idempotency",
    "inputHash",
    "publicInput",
    "reason",
    "allowedDecisions",
    "status",
    "decision",
    "resolvedByActorId",
    "resolvedByUserId",
    "requestedAt",
    "expiresAt",
    "resolvedAt",
)


class ApprovalError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def approval_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"approval_[A-Za-z0-9_-]{12,128}", value):
        raise ApprovalError("INVALID_APPROVAL", "Approval ID is invalid.")


def public_input(value):
    try:
        return public_json(value)
    except RuntimeStoreError as error:
        raise ApprovalError(
            "INVALID_APPROVAL", "Approval input must be a bounded public JSON object."
        ) from error


def view(row, fields=FIELDS):
    return {
        key: iso(row[snake(key)]) if isinstance(row[snake(key)], datetime) else row[snake(key)]
        for key in fields
    }


def integer_query(value, default):
    try:
        if value is None:
            return default
        parsed = float(value.strip() or "0")
        return int(parsed) if parsed.is_integer() and 0 <= parsed <= 9007199254740991 else default
    except (ValueError, TypeError, OverflowError):
        return default


class Approvals:
    def __init__(self, database):
        self.database = database

    async def create(self, request):
        approval_id(request["approvalId"])
        data = {
            "id": request["approvalId"],
            "run_id": request["runId"],
            "turn": request["turn"],
            "tool_call_id_hash": "sha256:"
            + hashlib.sha256(request["toolCallId"].encode()).hexdigest(),
            "tool_name": request["toolName"],
            "effect": request["effect"],
            "idempotency": request["idempotency"],
            "input_hash": "sha256:" + request["inputSha256"],
            "public_input": public_input(request["publicInput"]),
            "reason": request["reason"],
            "allowed_decisions": request["allowedDecisions"],
            "requested_at": datetime.fromtimestamp(request["requestedAt"] / 1000, UTC),
            "expires_at": datetime.fromtimestamp(request["expiresAt"] / 1000, UTC),
        }
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO agent_tool_approvals (id,run_id,turn,tool_call_id_hash,tool_name,effect,idempotency,input_hash,public_input,reason,allowed_decisions,requested_at,expires_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (id) DO NOTHING""",
                tuple(
                    Jsonb(value) if key == "public_input" else value for key, value in data.items()
                ),
            )
            row = await one(
                connection, "SELECT * FROM agent_tool_approvals WHERE id=%s", (data["id"],)
            )
            if any(
                (utc(row[key]) != utc(value)) if isinstance(value, datetime) else row[key] != value
                for key, value in data.items()
            ):
                raise ApprovalError(
                    "APPROVAL_CONFLICT", "Approval ID is already bound to a different request."
                )

    async def expire(self, project, run=None, identity=None):
        async with self.database.transaction() as connection:
            await connection.execute(
                """UPDATE agent_tool_approvals a SET status='expired',resolved_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP FROM agent_runs r
                WHERE a.run_id=r.id AND r.project_id=%s AND (%s::text IS NULL OR r.id=%s) AND (%s::text IS NULL OR a.id=%s)
                AND a.status='pending' AND (a.expires_at<=CURRENT_TIMESTAMP OR r.status<>'waiting')""",
                (project, run, run, identity, identity),
            )

    async def resolve(
        self,
        project,
        identity,
        actor,
        decision,
        *,
        actor_user=None,
        edited_input=None,
        edited_supplied=False,
    ):
        approval_id(identity)
        if (
            not isinstance(actor, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+/@=-]{0,255}", actor)
            or (actor_user is not None and actor_user != actor)
        ):
            raise ApprovalError("INVALID_APPROVAL", "Approval actor identity is invalid.")
        edited = public_input(edited_input) if decision == "edit" else None
        if decision not in {"approve", "edit", "reject"} or (
            decision != "edit" and edited_supplied
        ):
            raise ApprovalError(
                "INVALID_APPROVAL", "editedInput is allowed only for edit decisions."
            )
        expired = False
        async with self.database.transaction() as connection:
            binding = await one(
                connection, "SELECT run_id FROM agent_tool_approvals WHERE id=%s", (identity,)
            )
            run = (
                await one(
                    connection,
                    "SELECT project_id,status FROM agent_runs WHERE id=%s FOR UPDATE",
                    (binding["run_id"],),
                )
                if binding
                else None
            )
            row = (
                await one(
                    connection,
                    "SELECT * FROM agent_tool_approvals WHERE id=%s FOR UPDATE",
                    (identity,),
                )
                if binding
                else None
            )
            if not row or not run or run["project_id"] != project:
                raise ApprovalError(
                    "APPROVAL_NOT_FOUND", "Approval was not found for this project."
                )
            if decision not in row["allowed_decisions"]:
                raise ApprovalError(
                    "INVALID_APPROVAL", "Decision is not allowed for this approval."
                )
            if row["status"] != "pending":
                raise ApprovalError(
                    "APPROVAL_CONFLICT", "Approval is already " + row["status"] + "."
                )
            now = (await one(connection, "SELECT CURRENT_TIMESTAMP AS value"))["value"]
            if utc(row["expires_at"]) <= utc(now) or run["status"] != "waiting":
                await connection.execute(
                    "UPDATE agent_tool_approvals SET status='expired',resolved_at=%s,updated_at=%s WHERE id=%s",
                    (now, now, identity),
                )
                expired = True
            else:
                status = {"approve": "approved", "edit": "edited", "reject": "rejected"}[decision]
                await connection.execute(
                    "UPDATE agent_tool_approvals SET status=%s,decision=%s,edited_input=%s,resolved_by_actor_id=%s,resolved_by_user_id=%s,resolved_at=%s,updated_at=%s WHERE id=%s",
                    (
                        status,
                        decision,
                        Jsonb(edited) if edited is not None else None,
                        actor,
                        actor_user,
                        now,
                        now,
                        identity,
                    ),
                )
                result = {
                    "approvalId": identity,
                    "status": status,
                    "decision": decision,
                    "resolvedAt": iso(now),
                }
        if expired:
            raise ApprovalError("APPROVAL_EXPIRED", "Approval has expired.")
        return result

    async def list(self, project, *, run=None, status=None, limit=50):
        await self.expire(project, run)
        rows = await self.database.fetch_all(
            """SELECT a.* FROM agent_tool_approvals a JOIN agent_runs r ON r.id=a.run_id WHERE r.project_id=%s
            AND (%s::text IS NULL OR a.run_id=%s) AND (%s::text IS NULL OR a.status=%s)
            ORDER BY (a.status='pending') DESC,a.requested_at DESC LIMIT %s""",
            (project, run, run, status, status, max(1, min(limit, 200))),
        )
        rows.sort(key=lambda row: (PRIORITY[row["status"]], -row["requested_at"].timestamp()))
        return [view(row) for row in rows]

    async def wait(self, project, identity, *, cancel=None, poll_seconds=0.5):
        while True:
            if cancel and cancel.is_set():
                raise asyncio.CancelledError
            await self.expire(project, identity=identity)
            row = await self.database.fetch_one(
                "SELECT a.* FROM agent_tool_approvals a JOIN agent_runs r ON r.id=a.run_id WHERE a.id=%s AND r.project_id=%s",
                (identity, project),
            )
            if not row:
                raise ApprovalError(
                    "APPROVAL_NOT_FOUND", "Approval disappeared while awaiting a decision."
                )
            if row["status"] == "expired":
                return {"decision": "reject", "resolvedBy": "expired"}
            if row["status"] != "pending":
                if not row["resolved_by_actor_id"]:
                    raise ApprovalError(
                        "APPROVAL_CONFLICT", "Resolved approval is missing its actor principal."
                    )
                return {
                    "decision": row["decision"],
                    "resolvedBy": row["resolved_by_actor_id"],
                    **(
                        {"editedInput": public_input(row["edited_input"])}
                        if row["status"] == "edited"
                        else {}
                    ),
                }
            if cancel:
                with suppress(TimeoutError):
                    await asyncio.wait_for(cancel.wait(), poll_seconds)
            else:
                await asyncio.sleep(poll_seconds)

    async def timeline(self, project, run, after=0, limit=200):
        await self.expire(project, run)
        row = await self.database.fetch_one(
            "SELECT * FROM agent_runs WHERE id=%s AND project_id=%s", (run, project)
        )
        if not row:
            return None
        limit = max(1, min(limit, 1000))
        fields = (
            "id",
            "runInstanceId",
            "requestId",
            "status",
            "provider",
            "model",
            "frameworkVersion",
            "buildRevision",
            "turnCount",
            "inputTokens",
            "outputTokens",
            "totalTokens",
            "cachedInputTokens",
            "cacheMissInputTokens",
            "reasoningTokens",
            "lastEventSequence",
            "latestCheckpointSequence",
            "errorCode",
            "startedAt",
            "finishedAt",
            "createdAt",
            "updatedAt",
        )
        result = view(row, fields)
        result["runInstanceId"] = str(result["runInstanceId"])
        events = await self.database.fetch_all(
            "SELECT * FROM agent_events WHERE run_id=%s AND sequence>%s ORDER BY sequence LIMIT %s",
            (run, max(0, after), limit),
        )
        result["events"] = [
            view(record, ("eventId", "sequence", "eventType", "payload", "occurredAt"))
            for record in events
        ]
        checkpoint = await self.database.fetch_one(
            "SELECT * FROM agent_checkpoints WHERE run_id=%s ORDER BY sequence DESC LIMIT 1", (run,)
        )
        result["latestCheckpoint"] = (
            view(
                checkpoint,
                (
                    "sequence",
                    "turn",
                    "boundary",
                    "recoveryMode",
                    "publicState",
                    "stateHash",
                    "stateVersion",
                    "createdAt",
                ),
            )
            if checkpoint
            else None
        )
        approvals = await self.database.fetch_all(
            "SELECT * FROM agent_tool_approvals WHERE run_id=%s ORDER BY requested_at DESC", (run,)
        )
        result["toolApprovals"] = [
            view(record, tuple(key for key in FIELDS if key not in {"runId", "idempotency"}))
            for record in approvals
        ]
        result["hasMoreEvents"] = (
            len(events) == limit and events[-1]["sequence"] < row["last_event_sequence"]
        )
        return result


def approval_routes(services):
    router = APIRouter()

    @router.get("/api/projects/{project_id}/agent/approvals")
    async def list_approvals(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.read", project_id)
        status = request.query_params.get("status") or None
        if status and status not in STATUSES:
            return JSONResponse(
                {"success": False, "error": "INVALID_APPROVAL_STATUS"}, status_code=400
            )
        rows = await Approvals(database).list(
            project_id,
            run=request.query_params.get("runId") or None,
            status=status,
            limit=integer_query(request.query_params.get("limit"), 50),
        )
        return {"success": True, "data": rows}

    @router.post("/api/projects/{project_id}/agent/approvals/{approval_id}")
    async def resolve_approval(request: Request, project_id: str, approval_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.update", project_id)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict) or body.get("decision") not in {"approve", "edit", "reject"}:
            return JSONResponse(
                {"success": False, "error": "INVALID_APPROVAL_DECISION"}, status_code=400
            )
        result = await Approvals(database).resolve(
            project_id,
            approval_id,
            actor.actor_user_id,
            body["decision"],
            actor_user=None if actor.local_system_admin else actor.actor_user_id,
            edited_input=body.get("editedInput"),
            edited_supplied="editedInput" in body,
        )
        return {"success": True, "data": result}

    @router.get("/api/projects/{project_id}/agent/runs/{run_id}")
    async def timeline(request: Request, project_id: str, run_id: str):
        database, auth = services()
        await auth.require_action(request, "project.read", project_id)
        data = await Approvals(database).timeline(
            project_id,
            run_id,
            integer_query(request.query_params.get("afterSequence"), 0),
            integer_query(request.query_params.get("limit"), 200),
        )
        return (
            {"success": True, "data": data}
            if data
            else JSONResponse({"success": False, "error": "AGENT_RUN_NOT_FOUND"}, status_code=404)
        )

    return router
