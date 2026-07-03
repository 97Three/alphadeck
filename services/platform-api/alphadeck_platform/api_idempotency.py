import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from .api_errors import ApiError
from .generation_lease import database_now
from .messages import js_length, js_slice
from .provenance import js_json
from .quota import Quota, one, utc
from .runtime_store import insert
from .sessions import iso


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def document(value):
    ancestors, nodes = set(), 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 64 or nodes > 100000:
            raise ValueError("Idempotency payload exceeds its structural budget")
        if isinstance(item, datetime):
            return iso(item.astimezone(UTC))
        if item is None or type(item) in {str, bool, int, float}:
            return item
        if type(item) not in {dict, list} or id(item) in ancestors:
            raise ValueError("Idempotency payload must be acyclic JSON")
        ancestors.add(id(item))
        try:
            if isinstance(item, dict):
                return {
                    key: visit(item[key], depth + 1)
                    for key in sorted(
                        item, key=lambda key: key.encode("utf-16-be", "surrogatepass")
                    )
                }
            return [visit(child, depth + 1) for child in item]
        finally:
            ancestors.remove(id(item))

    text = js_json(visit(value))
    return json.loads(text), text.encode()


def normalize_key(value):
    candidate = value.strip()
    return (
        "raw:" + candidate
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", candidate)
        else "sha256:" + digest(candidate)
    )


def handle(row):
    return {"id": row["id"], "attempt": row["attempt"], "payloadHash": row["payload_hash"]}


class ApiOperations:
    def __init__(self, database):
        self.database = database

    async def claim(
        self, scope, actor, key, payload, *, lease_seconds=21600, retention_seconds=604800, now=None
    ):
        for value, maximum in ((scope, 120), (actor, 160), (key, 512)):
            if not isinstance(value, str) or not 0 < js_length(value.strip()) <= maximum:
                raise ValueError("Invalid API idempotency identity")
        scope, actor, key = scope.strip(), actor.strip(), key.strip()
        key_hash, payload_hash = digest(key), hashlib.sha256(document(payload)[1]).hexdigest()
        lease_seconds = max(60, min(86400, lease_seconds))
        retention_seconds = max(3600, min(2592000, retention_seconds))
        async with self.database.transaction() as tx:
            await tx.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("\x1f".join((scope, actor, key_hash)),),
            )
            now = now or await database_now(tx)
            row = await one(
                tx,
                "SELECT * FROM api_idempotency_operations WHERE scope=%s AND actor_key=%s AND idempotency_key_hash=%s FOR UPDATE",
                (scope, actor, key_hash),
            )
            expiry, retention = (
                now + timedelta(seconds=lease_seconds),
                now + timedelta(seconds=retention_seconds),
            )
            if not row:
                row = await insert(
                    tx,
                    "api_idempotency_operations",
                    {
                        "id": str(uuid4()),
                        "scope": scope,
                        "actor_key": actor,
                        "idempotency_key_hash": key_hash,
                        "payload_hash": payload_hash,
                        "lease_expires_at": expiry,
                        "retention_expires_at": retention,
                        "updated_at": now,
                    },
                )
            else:
                if row["payload_hash"] != payload_hash:
                    raise ApiError(
                        409,
                        "IDEMPOTENCY_PAYLOAD_CONFLICT",
                        "该幂等键已绑定到不同的请求参数，请为新请求使用新的幂等键。",
                    )
                pending_accounting = (
                    row["quota_settlement"] is not None and row["quota_accounted_at"] is None
                )
                if row["status"] == "completed" and (
                    utc(row["retention_expires_at"]) > utc(now) or pending_accounting
                ):
                    return {
                        "state": "completed",
                        "handle": handle(row),
                        "responseStatus": row["response_status"] or 200,
                        "responseBody": row["response_body"],
                        "responseAvailable": row["response_body"] is not None,
                    }
                if row["status"] == "running" and utc(row["lease_expires_at"]) > utc(now):
                    return {
                        "state": "in_progress",
                        "retryAfterSeconds": max(
                            1, math.ceil((utc(row["lease_expires_at"]) - utc(now)).total_seconds())
                        ),
                    }
                row = await one(
                    tx,
                    """UPDATE api_idempotency_operations SET status='running',attempt=attempt+1,lease_expires_at=%s,retention_expires_at=%s,
                    response_status=NULL,response_body=NULL,response_bytes=NULL,quota_reservation_id=NULL,quota_settlement=NULL,quota_accounted_at=NULL,
                    error_code=NULL,error_message=NULL,completed_at=NULL,updated_at=%s WHERE id=%s RETURNING *""",
                    (expiry, retention, now, row["id"]),
                )
            return {"state": "acquired", "handle": handle(row)}

    async def complete(
        self,
        claim,
        response_status,
        response_body,
        *,
        cache=True,
        settlement=None,
        retention_seconds=604800,
        now=None,
    ):
        value, serialized = document(response_body)
        available = cache and len(serialized) <= 524288
        if settlement:
            settlement = {**settlement, "actualQuantity": str(settlement["actualQuantity"])}
            if "additionalUsage" in settlement:
                settlement["additionalUsage"] = [
                    {**item, "quantity": str(item["quantity"])}
                    for item in settlement["additionalUsage"]
                ]
            settlement = document(settlement)[0]
        async with self.database.transaction() as tx:
            now = now or await database_now(tx)
            changed = await tx.execute(
                """UPDATE api_idempotency_operations SET status='completed',response_status=%s,response_body=%s,response_bytes=%s,
                quota_reservation_id=%s,quota_settlement=%s,quota_accounted_at=NULL,completed_at=%s,lease_expires_at=%s,retention_expires_at=%s,error_code=NULL,error_message=NULL,updated_at=%s
                WHERE id=%s AND attempt=%s AND payload_hash=%s AND status='running'""",
                (
                    response_status,
                    Jsonb(value) if available else None,
                    len(serialized),
                    settlement["reservationId"] if settlement else None,
                    Jsonb(settlement) if settlement else None,
                    now,
                    now,
                    now + timedelta(seconds=max(3600, min(2592000, retention_seconds))),
                    now,
                    claim["id"],
                    claim["attempt"],
                    claim["payloadHash"],
                ),
            )
            if changed.rowcount != 1:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CLAIM_LOST",
                    "API idempotency claim was lost before completion.",
                )
        return {"responseAvailable": available, "responseBytes": len(serialized)}

    async def fail(self, claim, error, *, now=None):
        async with self.database.transaction() as tx:
            now = now or await database_now(tx)
            result = await tx.execute(
                """UPDATE api_idempotency_operations SET status='failed',lease_expires_at=%s,error_code=%s,error_message=%s,updated_at=%s
                WHERE id=%s AND attempt=%s AND payload_hash=%s AND status='running'""",
                (
                    now,
                    js_slice(str(error.code), 0, 120) if getattr(error, "code", None) else None,
                    js_slice(str(error), 0, 500),
                    now,
                    claim["id"],
                    claim["attempt"],
                    claim["payloadHash"],
                ),
            )
            return result.rowcount == 1

    async def mark_accounted(self, claim, reservation, *, now=None):
        async with self.database.transaction() as tx:
            now = now or await database_now(tx)
            result = await tx.execute(
                "UPDATE api_idempotency_operations SET quota_accounted_at=%s,updated_at=%s WHERE id=%s AND attempt=%s AND payload_hash=%s AND status='completed' AND quota_reservation_id=%s AND quota_accounted_at IS NULL",
                (now, now, claim["id"], claim["attempt"], claim["payloadHash"], reservation),
            )
            return result.rowcount == 1

    async def cleanup(self, *, now=None):
        async with self.database.transaction() as tx:
            now = now or await database_now(tx)
            result = await tx.execute(
                """DELETE FROM api_idempotency_operations WHERE retention_expires_at<=%s AND
                (status='failed' OR (status='completed' AND (quota_accounted_at IS NOT NULL OR quota_settlement IS NULL)) OR (status='running' AND lease_expires_at<=%s))""",
                (now, now),
            )
            return result.rowcount

    async def reconcile(self, *, limit=1000, on_error=None):
        rows = await self.database.fetch_all(
            "SELECT * FROM api_idempotency_operations WHERE status='completed' AND quota_accounted_at IS NULL AND quota_settlement IS NOT NULL ORDER BY completed_at LIMIT %s",
            (max(1, min(1000, limit)),),
        )
        reconciled, failed = 0, 0
        for row in rows:
            try:
                await self.account(row)
                reconciled += int(
                    await self.mark_accounted(handle(row), row["quota_reservation_id"])
                )
            except Exception as error:
                failed += 1
                if on_error:
                    on_error(row["id"], error)
        return {"scanned": len(rows), "reconciled": reconciled, "failed": failed}

    async def account(self, operation):
        plan = operation["quota_settlement"]
        required = (
            "reservationId",
            "actorUserId",
            "metric",
            "actualQuantity",
            "sourceType",
            "usageEventIdempotencyKey",
        )
        if not isinstance(plan, dict) or any(
            not isinstance(plan.get(key), str) or not plan[key] for key in required
        ):
            raise ValueError("Invalid API operation quota settlement plan")
        actor, reservation_id, quantity = (
            plan["actorUserId"],
            plan["reservationId"],
            int(plan["actualQuantity"]),
        )
        if quantity < 0 or actor != operation["actor_key"]:
            raise ValueError("API operation actor or quantity does not match its quota plan")
        reservation = await self.database.fetch_one(
            "SELECT * FROM quota_reservations WHERE id=%s", (reservation_id,)
        )
        if reservation and (
            reservation["actor_user_id"],
            reservation["metric"],
            reservation["project_id"],
        ) != (actor, plan["metric"], plan.get("projectId")):
            raise ValueError(
                "Quota reservation identity does not match the persisted settlement plan"
            )
        occurred = (
            datetime.fromisoformat(plan["occurredAt"].replace("Z", "+00:00"))
            if isinstance(plan.get("occurredAt"), str)
            else operation["completed_at"] or operation["created_at"]
        )
        quota = Quota(self.database)
        if (
            reservation
            and reservation["status"] == "active"
            and utc(reservation["expires_at"]) > datetime.now(UTC)
        ):
            await quota.settle(
                reservation_id,
                quantity,
                plan["sourceType"],
                source_id=plan.get("sourceId"),
                event_key=plan["usageEventIdempotencyKey"],
                at=occurred,
                metadata=plan.get("metadata"),
            )
        elif not reservation or reservation["status"] != "settled":
            await quota.record_usage(
                actor,
                plan["metric"],
                quantity,
                plan["usageEventIdempotencyKey"],
                plan["sourceType"],
                source_id=plan.get("sourceId"),
                project_id=plan.get("projectId"),
                at=occurred,
                metadata=plan.get("metadata"),
            )
        for usage in plan.get("additionalUsage", []):
            if usage["actorUserId"] != actor or int(usage["quantity"]) < 0:
                raise ValueError("Additional quota usage does not match the operation actor")
            await quota.record_usage(
                actor,
                usage["metric"],
                int(usage["quantity"]),
                usage["idempotencyKey"],
                usage["sourceType"],
                source_id=usage.get("sourceId"),
                project_id=usage.get("projectId"),
                at=datetime.fromisoformat(usage["occurredAt"].replace("Z", "+00:00"))
                if isinstance(usage.get("occurredAt"), str)
                else occurred,
                metadata=usage.get("metadata"),
            )
