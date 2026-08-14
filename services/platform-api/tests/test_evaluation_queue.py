import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from alphadeck_platform.database import Database
from alphadeck_platform.evaluation_queue import EvaluationQueue, date, queue_view, virtual_item
from alphadeck_platform.runtime_store import snake

CASES = json.loads((Path(__file__).parent / "fixtures/evaluation-queue.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_evaluation_queue_response_matches_original(case):
    row = {snake(key): value for key, value in case["input"].items()}
    for key in ("created_at", "started_at", "finished_at"):
        row[key] = date(row[key])
    assert queue_view(row) == case["expected"]


@pytest.fixture
def queue(database_url):
    database = Database(database_url)
    prefix = "eval-native-" + uuid4().hex + "-"
    yield EvaluationQueue(database), prefix
    with psycopg.connect(database_url) as tx:
        tx.execute("DELETE FROM eval_queue_items WHERE id LIKE %s", (prefix + "%",))
        tx.execute("DELETE FROM eval_schedules WHERE id='default'")


async def enqueue(store, prefix, count=2):
    return await asyncio.gather(
        *(store.enqueue({**virtual_item(), "id": prefix + str(index)}) for index in range(count))
    )


def test_benchmark_claims_are_exclusive_and_fenced_across_clients(queue):
    store, prefix = queue

    async def run():
        await enqueue(store, prefix)
        clients = [EvaluationQueue(store.database) for _ in range(6)]
        claims = await asyncio.gather(*(client.claim() for client in clients))
        assert sum(claim is not None for claim in claims) == 1
        claim = next(item for item in claims if item)
        forged = {**claim, "leaseToken": str(uuid4())}
        assert not await clients[1].heartbeat(forged)
        assert not await clients[1].record_process(forged, 999, "wrong.log")
        assert not await clients[1].finish(
            forged, {"status": "passed", "exitCode": 0, "error": None}
        )
        assert await store.heartbeat(claim)
        assert await store.record_process(claim, 1, "tmp/eval/fixture.log")
        assert await store.finish(claim, {"status": "passed", "exitCode": 0, "error": None})
        assert not await store.finish(claim, {"status": "failed", "error": "late event"})
        assert await store.claim()

    asyncio.run(run())


def test_benchmark_cancellation_holds_capacity_until_owning_process_stops(queue):
    store, prefix = queue

    async def run():
        items = await enqueue(store, prefix)
        claim = await store.claim()
        assert (await store.cancel(items[0]["id"]))["status"] == "cancelled"
        assert not await store.heartbeat(claim)
        assert not await store.claim()
        assert not await store.finish(claim, {"status": "passed", "exitCode": 0})
        await store.release_cancelled({**claim, "leaseToken": "wrong"})
        assert not await store.claim()
        await store.release_cancelled(claim)
        assert (await store.claim())["item"]["id"] == items[1]["id"]

    asyncio.run(run())


def test_expired_benchmark_is_failed_without_replaying_paid_work(queue):
    store, prefix = queue

    async def run():
        items = await enqueue(store, prefix)
        claim = await store.claim()
        async with store.database.transaction() as tx:
            await tx.execute(
                "UPDATE eval_queue_items SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (items[0]["id"],),
            )
        assert (await store.claim())["item"]["id"] == items[1]["id"]
        assert not await store.heartbeat(claim)
        assert not await store.finish(claim, {"status": "passed", "exitCode": 0})
        row = await store.database.fetch_one(
            "SELECT status,attempt_count,lease_token FROM eval_queue_items WHERE id=%s",
            (items[0]["id"],),
        )
        assert row == {"status": "failed", "attempt_count": 1, "lease_token": None}

    asyncio.run(run())


def test_benchmark_claim_is_not_limited_to_dashboard_window(queue):
    store, prefix = queue

    async def run():
        oldest = {**virtual_item(now=datetime(2020, 1, 1, tzinfo=UTC)), "id": prefix + "oldest"}
        await store.enqueue(oldest)
        await enqueue(store, prefix, 85)
        assert len(await store.list()) == 80
        assert not any(item["id"] == oldest["id"] for item in await store.list())
        assert (await store.claim())["item"]["id"] == oldest["id"]

    asyncio.run(run())


def test_due_schedule_is_advanced_and_enqueued_exactly_once(queue):
    store, prefix = queue

    async def run():
        schedule = await store.update_schedule(
            {"enabled": True, "intervalHours": 24, "nextRunAt": "1970-01-01T00:00:00Z"}
        )
        assert schedule["enabled"]
        results = await asyncio.gather(
            *(EvaluationQueue(store.database).check_schedule() for _ in range(6))
        )
        queued = [item for item in results if item["queued"]]
        assert len(queued) == 1
        item = queued[0]["item"]
        try:
            assert item["status"] == "queued"
            assert queued[0]["schedule"]["lastQueuedRunId"] == item["id"]
            assert not (await store.check_schedule())["queued"]
            disabled = await store.update_schedule({"enabled": False})
            assert disabled["nextRunAt"] is None
        finally:
            async with store.database.transaction() as tx:
                await tx.execute("DELETE FROM eval_queue_items WHERE id=%s", (item["id"],))

    asyncio.run(run())
