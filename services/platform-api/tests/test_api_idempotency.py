import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from alphadeck_platform.api_errors import ApiError
from alphadeck_platform.api_idempotency import ApiOperations, document, normalize_key
from alphadeck_platform.database import Database
from alphadeck_platform.quota import Quota


@pytest.fixture
def operations(database_url):
    database, scope = Database(database_url), "contract-" + uuid4().hex
    store = ApiOperations(database)
    yield store, database, scope

    async def cleanup():
        async with database.transaction() as tx:
            await tx.execute("DELETE FROM api_idempotency_operations WHERE scope=%s", (scope,))

    asyncio.run(cleanup())


def test_api_idempotency_concurrent_claims_actor_isolation_and_replay(operations):
    store, _, scope = operations

    async def run():
        claims = await asyncio.gather(
            *(store.claim(scope, "actor", "key", {"b": 2, "a": 1}) for _ in range(8))
        )
        assert sum(claim["state"] == "acquired" for claim in claims) == 1
        assert sum(claim["state"] == "in_progress" for claim in claims) == 7
        claim = next(claim["handle"] for claim in claims if claim["state"] == "acquired")
        with pytest.raises(ApiError) as conflict:
            await store.claim(scope, "actor", "key", {"a": 2, "b": 2})
        assert conflict.value.code == "IDEMPOTENCY_PAYLOAD_CONFLICT"
        assert (await store.claim(scope, "other", "key", {"a": 2}))["state"] == "acquired"
        await store.complete(claim, 200, {"result": "研究结果", "count": 1})
        replay = await store.claim(scope, "actor", "key", {"a": 1, "b": 2})
        assert replay["state"] == "completed" and replay["responseAvailable"]
        assert replay["responseBody"] == {"result": "研究结果", "count": 1}
        assert not await store.fail(claim, RuntimeError("late failure"))

    asyncio.run(run())


def test_api_idempotency_attempt_fences_and_uncached_result_are_durable(operations):
    store, _, scope = operations

    async def run():
        first_at = datetime(2026, 11, 1, tzinfo=UTC)
        first = await store.claim(scope, "actor", "retry", {}, lease_seconds=60, now=first_at)
        second = await store.claim(
            scope, "actor", "retry", {}, now=first_at + timedelta(seconds=61)
        )
        assert second["handle"]["attempt"] == 2
        with pytest.raises(ApiError, match="lost before completion"):
            await store.complete(first["handle"], 200, {})
        assert not await store.fail(first["handle"], RuntimeError("stale failure"))
        response = await store.complete(second["handle"], 503, {"large": "x" * 524288})
        assert not response["responseAvailable"] and response["responseBytes"] > 524288
        replay = await store.claim(scope, "actor", "retry", {})
        assert (
            replay["state"] == "completed"
            and replay["responseStatus"] == 503
            and not replay["responseAvailable"]
        )

    asyncio.run(run())


def test_api_operation_accounting_repair_never_repeats_business_operation(
    operations, actor, monkeypatch
):
    store, database, scope = operations
    original_record = Quota.record_usage
    broken = True

    async def intermittent(self, *args, **kwargs):
        nonlocal broken
        if broken:
            broken = False
            raise OSError("fixture token ledger unavailable")
        return await original_record(self, *args, **kwargs)

    monkeypatch.setattr(Quota, "record_usage", intermittent)

    async def run():
        claim = (await store.claim(scope, actor["user_id"], "accounting", {}))["handle"]
        reservation = (
            await Quota(database).reserve(
                actor["user_id"], "query_rewrite.llm.daily", 1, scope + ":reservation"
            )
        )["reservation"]["id"]
        settlement = {
            "reservationId": reservation,
            "actorUserId": actor["user_id"],
            "metric": "query_rewrite.llm.daily",
            "actualQuantity": 1,
            "sourceType": "query_rewrite",
            "sourceId": scope,
            "usageEventIdempotencyKey": scope + ":request",
            "additionalUsage": [
                {
                    "actorUserId": actor["user_id"],
                    "metric": "llm.total_tokens.monthly",
                    "quantity": 15,
                    "idempotencyKey": scope + ":tokens",
                    "sourceType": "query_rewrite",
                    "sourceId": scope,
                }
            ],
        }
        await store.complete(claim, 200, {"result": "already executed"}, settlement=settlement)
        result = await store.reconcile()
        assert result["failed"] >= 1
        replay = await store.claim(scope, actor["user_id"], "accounting", {})
        assert replay["state"] == "completed"
        result = await store.reconcile()
        assert result["reconciled"] >= 1
        assert (await store.reconcile())["reconciled"] == 0
        rows = await database.fetch_all(
            "SELECT metric,quantity FROM usage_events WHERE source_id=%s ORDER BY metric", (scope,)
        )
        assert rows == [
            {"metric": "llm.total_tokens.monthly", "quantity": 15},
            {"metric": "query_rewrite.llm.daily", "quantity": 1},
        ]
        row = await database.fetch_one(
            "SELECT quota_accounted_at FROM api_idempotency_operations WHERE id=%s", (claim["id"],)
        )
        assert row["quota_accounted_at"] is not None

    asyncio.run(run())


def test_idempotency_key_representation_and_json_number_semantics():
    assert normalize_key(" key ") == "raw:key"
    long_key = "x" * 121
    normalized = normalize_key(long_key)
    assert normalized.startswith("sha256:")
    assert normalize_key(normalized) != normalized
    _, serialized = document({"2": 2, "10": 10, "a": [1.0, -0.0, 1e-7]})
    assert serialized == b'{"2":2,"10":10,"a":[1,0,1e-7]}'
