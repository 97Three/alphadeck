"""Deduplicate costly platform operations and persist quota settlement intent before replying."""

from contextlib import suppress
from uuid import uuid4

from .api_idempotency import ApiOperations, normalize_key
from .messages import MessageResponse
from .quota import Quota


async def metered_operation(
    database,
    context,
    *,
    scope,
    action,
    payload,
    requested_key,
    units=0,
    metric="quant.data_units.daily",
    source_type="strategy_api",
    execute,
):
    actor = context.actor_user_id if context.user else None
    explicit = bool(requested_key.strip())
    key = normalize_key(requested_key) if explicit else str(uuid4())
    operations, quota, claim, reservation = ApiOperations(database), Quota(database), None, None
    if explicit or actor and units > 0:
        state = await operations.claim(
            scope,
            actor or "anonymous",
            key,
            payload,
            retention_seconds=604800 if explicit else 3600,
        )
        if state["state"] == "in_progress":
            return MessageResponse(
                {
                    "success": False,
                    "error": "IDEMPOTENCY_IN_PROGRESS",
                    "message": "相同幂等键的操作正在执行，请稍后重试。",
                },
                status_code=409,
                headers={"Retry-After": str(min(30, state["retryAfterSeconds"]))},
            )
        if state["state"] == "completed":
            if not state["responseAvailable"] or state["responseBody"] is None:
                return MessageResponse(
                    {
                        "success": False,
                        "error": "IDEMPOTENCY_RESULT_UNAVAILABLE",
                        "message": "该操作已完成，但响应未进入幂等缓存；本次不会重复执行。",
                    },
                    status_code=409,
                )
            return MessageResponse(
                state["responseBody"],
                status_code=state["responseStatus"],
                headers={"Idempotency-Replayed": "true"},
            )
        claim = state["handle"]
    attempt = claim["attempt"] if claim else 1
    prefix = "strategy" if scope == "quant-strategies" else "research"
    identity = f"{prefix}:{actor}:{action}:{key}:attempt:{attempt}"
    if actor and units > 0:
        try:
            decision = await quota.reserve(actor, metric, units, identity)
            reservation = (decision.get("reservation") or {}).get("id")
        except Exception as cause:
            if claim:
                with suppress(Exception):
                    await operations.fail(claim, cause)
            raise
    try:
        body, status = await execute(key)
    except BaseException as cause:
        if reservation:
            with suppress(Exception):
                await quota.release(reservation)
        if claim:
            with suppress(Exception):
                await operations.fail(claim, cause)
        raise
    if not 200 <= status < 300:
        if reservation:
            with suppress(Exception):
                await quota.release(reservation)
        if claim:
            with suppress(Exception):
                await operations.fail(
                    claim, ValueError("Business operation returned HTTP " + str(status))
                )
        return MessageResponse(body, status_code=status)
    settlement = (
        {
            "reservationId": reservation,
            "actorUserId": actor,
            "metric": metric,
            "actualQuantity": str(units),
            "sourceType": source_type,
            "sourceId": key,
            "usageEventIdempotencyKey": identity + ":usage",
            "metadata": {"action": action, "units": units, "attempt": attempt},
        }
        if reservation
        else None
    )
    if claim:
        try:
            await operations.complete(claim, status, body, cache=explicit, settlement=settlement)
        except Exception:
            # The business side effect may already have happened. Keep its claim
            # and reservation; re-executing it here could duplicate a paid task.
            return MessageResponse(
                {
                    "success": False,
                    "error": "IDEMPOTENCY_COMMIT_FAILED",
                    "message": "操作已执行，但幂等结果暂时无法安全保存，请使用相同幂等键重试。",
                },
                status_code=503,
            )
    if settlement:
        with suppress(Exception):
            if claim:
                row = await database.fetch_one(
                    "SELECT * FROM api_idempotency_operations WHERE id=%s", (claim["id"],)
                )
                await operations.account(row)
                await operations.mark_accounted(claim, reservation)
            else:
                await quota.settle(
                    reservation,
                    units,
                    source_type,
                    source_id=key,
                    event_key=identity + ":usage",
                    metadata={"action": action, "units": units, "attempt": attempt},
                )
    return MessageResponse(body, status_code=status)
