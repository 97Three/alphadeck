import math
import time
import unicodedata
from contextlib import suppress
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .api_errors import ApiError
from .api_idempotency import ApiOperations, normalize_key
from .config import market_base_url
from .messages import js_length, parsed_integer
from .query_rewrite import rewrite_failure, rewrite_query
from .quota import Quota


def error_response(code, message, status, *, retryable=False, retry_after=None):
    return JSONResponse(
        {"success": False, "error": {"code": code, "message": message, "retryable": retryable}},
        status_code=status,
        headers={"Retry-After": str(min(30, retry_after))} if retry_after else None,
    )


def quant_routes(services, *, rewriter=None, market_client=None):
    router = APIRouter()

    @router.get("/api/quant/symbols/resolve")
    async def resolve(request: Request):
        database, auth = services()
        context = await auth.require_action(request, "quant.data.read")
        query = unicodedata.normalize("NFKC", request.query_params.get("query", "")).strip()
        count = parsed_integer(request.query_params.get("count"), 3, 1, 5)
        if not 2 <= js_length(query) <= 64:
            return JSONResponse(
                {"success": False, "error": "证券名称或代码长度必须在 2 到 64 个字符之间。"},
                status_code=400,
            )
        if context.user:
            identity = str(uuid4())
            await Quota(database).consume(
                context.actor_user_id,
                "quant.data_units.daily",
                1,
                f"symbol-resolve:{context.actor_user_id}:{identity}",
                "symbol_resolve",
                source_id=identity,
                event_key=f"symbol-resolve:{context.actor_user_id}:{identity}:usage",
            )
        base = market_base_url()
        owned = market_client is None
        client = market_client or httpx.AsyncClient(
            timeout=3.5, trust_env=False, follow_redirects=False
        )
        try:
            response = await client.get(
                base + "/api/v1/symbols/resolve",
                params={"query": query, "count": count},
                timeout=3.5,
            )
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if not response.is_success:
                return JSONResponse(
                    {"success": False, "error": "暂时无法解析证券标的。", "detail": payload},
                    status_code=response.status_code,
                )
            return {"success": True, "data": payload}
        except httpx.HTTPError as cause:
            return JSONResponse(
                {
                    "success": False,
                    "error": "证券解析超时，请稍后重试。"
                    if isinstance(cause, httpx.TimeoutException)
                    else "证券解析服务暂不可用。",
                },
                status_code=503,
            )
        finally:
            if owned:
                await client.aclose()

    @router.post("/api/quant/query/rewrite")
    async def rewrite(request: Request):
        started = time.monotonic()
        try:
            body = await request.json()
        except ValueError:
            return error_response("INVALID_JSON", "请求体必须是有效的 JSON。", 400)
        if not isinstance(body, dict):
            return error_response("INVALID_REQUEST", "请求体必须是 JSON 对象。", 400)
        query = (
            unicodedata.normalize("NFKC", body["query"]).strip()
            if isinstance(body.get("query"), str)
            else ""
        )
        capability = (
            body["requestedCapabilityId"].strip()
            if isinstance(body.get("requestedCapabilityId"), str)
            else None
        )
        model = body["model"].strip() if isinstance(body.get("model"), str) else None
        purpose = body.get("purpose", "execution")
        if purpose not in ("preview", "execution"):
            return error_response("INVALID_PURPOSE", "purpose 必须是 preview 或 execution。", 400)
        database, auth = services()
        context = await auth.require_action(request, "quant.query.rewrite.llm")
        if not 2 <= js_length(query) <= 2000:
            return error_response("INVALID_QUERY", "query 长度必须在 2 到 2000 个字符之间。", 400)
        key = request.headers.get(
            "idempotency-key",
            body.get("requestId", "") if isinstance(body.get("requestId"), str) else "",
        ).strip()
        explicit = bool(key)
        identity = normalize_key(key) if explicit else str(uuid4())
        public_identity = key if explicit else identity
        operations, quota, claim = ApiOperations(database), Quota(database), None
        if explicit or context.user:
            try:
                operation = await operations.claim(
                    "quant-query-rewrite",
                    context.actor_user_id if context.user else "anonymous",
                    identity,
                    {
                        "query": query,
                        "requestedCapabilityId": capability,
                        "purpose": purpose,
                        "requestedModel": model,
                    },
                    lease_seconds=600,
                    retention_seconds=604800 if explicit else 3600,
                )
                if operation["state"] == "in_progress":
                    return error_response(
                        "IDEMPOTENCY_IN_PROGRESS",
                        "相同幂等键的 Query Rewrite 正在执行，请稍后重试。",
                        409,
                        retryable=True,
                        retry_after=operation["retryAfterSeconds"],
                    )
                if operation["state"] == "completed":
                    if not operation["responseAvailable"] or operation["responseBody"] is None:
                        return error_response(
                            "IDEMPOTENCY_RESULT_UNAVAILABLE",
                            "该请求已经完成，但响应过大未进入幂等缓存；为避免重复调用模型，本次不会重新执行。",
                            409,
                        )
                    return JSONResponse(
                        operation["responseBody"],
                        status_code=operation["responseStatus"],
                        headers={"Idempotency-Replayed": "true"},
                    )
                claim = operation["handle"]
            except ApiError as cause:
                return error_response(cause.code, cause.message, cause.status)
            except Exception:
                return error_response(
                    "IDEMPOTENCY_CHECK_FAILED", "暂时无法确认请求幂等状态，请稍后重试。", 503
                )
        reservation = None
        if context.user:
            try:
                decision = await quota.reserve(
                    context.actor_user_id,
                    "query_rewrite.llm.daily",
                    1,
                    f"query-rewrite:{context.actor_user_id}:{identity}:attempt:{claim['attempt'] if claim else 1}",
                )
                reservation = decision["reservation"]["id"]
            except Exception as cause:
                if claim:
                    with suppress(Exception):
                        await operations.fail(claim, cause)
                raise
        try:
            data = await (rewriter or rewrite_query)(
                query, {"requestedCapabilityId": capability, "requestedModel": model}
            )
        except Exception as cause:
            if reservation:
                with suppress(Exception):
                    await quota.release(reservation)
            if claim:
                with suppress(Exception):
                    await operations.fail(claim, cause)
            raise
        failure = rewrite_failure(data)
        response_status = 503 if failure else 200
        response_body = {
            "success": failure is None,
            **({"error": failure} if failure else {}),
            "data": data,
            "meta": {
                "schemaVersion": data["schemaVersion"],
                "durationMs": max(0, math.floor((time.monotonic() - started) * 1000 + 0.5)),
                "purpose": purpose,
                "strategy": data["execution"]["strategy"],
                "llmStatus": data["execution"]["llm"]["status"],
                "safetyDecision": data["safety"]["decision"],
                "requestId": public_identity,
            },
        }
        llm, settlement = data["execution"]["llm"], None
        if reservation and context.user:
            actor, attempt = context.actor_user_id, claim["attempt"] if claim else 1
            settlement = {
                "reservationId": reservation,
                "actorUserId": actor,
                "metric": "query_rewrite.llm.daily",
                "actualQuantity": "1" if llm["attempted"] else "0",
                "sourceType": "query_rewrite",
                "sourceId": identity,
                "usageEventIdempotencyKey": f"query-rewrite:{actor}:{identity}:attempt:{attempt}:request",
                "metadata": {"status": llm["status"], "strategy": data["execution"]["strategy"]},
            }
            if llm.get("usage") and llm["usage"]["totalTokens"] > 0:
                settlement["additionalUsage"] = [
                    {
                        "actorUserId": actor,
                        "metric": "llm.total_tokens.monthly",
                        "quantity": str(llm["usage"]["totalTokens"]),
                        "idempotencyKey": f"query-rewrite:{actor}:{identity}:attempt:{attempt}:tokens",
                        "sourceType": "query_rewrite",
                        "sourceId": identity,
                        "metadata": {
                            "provider": llm["provider"],
                            "model": llm["model"],
                            "inputTokens": llm["usage"]["inputTokens"],
                            "outputTokens": llm["usage"]["outputTokens"],
                        },
                    }
                ]
        if claim:
            try:
                await operations.complete(
                    claim, response_status, response_body, cache=explicit, settlement=settlement
                )
            except Exception:
                return error_response(
                    "IDEMPOTENCY_COMMIT_FAILED",
                    "Query Rewrite 已执行，但暂时无法安全保存幂等结果，请稍后使用相同幂等键重试。",
                    503,
                )
        if settlement and claim:
            # A failed accounting write leaves a durable intent. Reconciliation never calls the model.
            with suppress(Exception):
                row = await database.fetch_one(
                    "SELECT * FROM api_idempotency_operations WHERE id=%s", (claim["id"],)
                )
                await operations.account(row)
                await operations.mark_accounted(claim, reservation)
        return JSONResponse(response_body, status_code=response_status)

    return router
