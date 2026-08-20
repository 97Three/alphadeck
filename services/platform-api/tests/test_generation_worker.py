import asyncio
from uuid import uuid4

import psycopg
import pytest

from alphadeck_platform.database import Database
from alphadeck_platform.dispatch import Dispatch
from alphadeck_platform.generation_worker import GenerationWorker
from alphadeck_platform.maintenance import reconcile_run_usage
from alphadeck_platform.runtime_store import RuntimeStore, fence_for
from alphadeck_platform.user_requests import UserRequests


@pytest.fixture
def worker_jobs(database_url, actor):
    database, pool = Database(database_url), "generation.test." + uuid4().hex
    projects = ["worker-" + uuid4().hex for _ in range(2)]
    requests = [str(uuid4()), str(uuid4())]
    with psycopg.connect(database_url) as tx:
        for project, request in zip(projects, requests, strict=True):
            tx.execute(
                "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Worker contract',%s,now())",
                (project, actor["user_id"]),
            )
            tx.execute(
                "INSERT INTO user_requests (id,project_id,actor_user_id,instruction) VALUES (%s,%s,%s,'fixture domain operation')",
                (request, project, actor["user_id"]),
            )

    async def enqueue():
        return [
            await Dispatch(database).enqueue(
                project,
                request,
                "fixture domain operation",
                stage="planning_data_prefetch",
                execution_envelope={"schemaVersion": 1, "fixture": True},
            )
            for project, request in zip(projects, requests, strict=True)
        ]

    jobs = asyncio.run(enqueue())
    yield database, pool, jobs
    with psycopg.connect(database_url) as tx:
        tx.execute("DELETE FROM agent_worker_slots WHERE pool_key=%s", (pool,))
        tx.execute("DELETE FROM agent_worker_instances WHERE pool_key=%s", (pool,))
        tx.execute("DELETE FROM projects WHERE id=ANY(%s)", (projects,))


def test_native_worker_registers_claims_checkpoints_and_releases_capacity(worker_jobs):
    database, pool, jobs = worker_jobs

    async def run():
        calls = []

        class FixtureDomainRuntime:
            async def execute(self, input, *, fence):
                calls.append(input)
                assert input["executionEnvelope"]["fixture"] is True
                await Dispatch(database).checkpoint(
                    fence, {"schemaVersion": 1, "fixturePrepared": True}
                )
                assert (await Dispatch(database).read_claimed(fence))["stage"] == "agent_execution"
                # This fixture is a host-controlled domain action; it does not claim finance acceptance.
                await Dispatch(database).finish(
                    input["projectId"], input["requestId"], "completed", fence=fence
                )
                await UserRequests(database).status(
                    input["projectId"], input["requestId"], "completed"
                )

        worker = GenerationWorker(database, FixtureDomainRuntime(), pool=pool)
        await worker.run(once=True)
        assert len(calls) == 2
        assert {item["jobId"] for item in calls} == {str(job["id"]) for job in jobs}
        rows = await database.fetch_all(
            "SELECT status,attempt_count,stage FROM agent_generation_jobs WHERE id=ANY(%s)",
            ([job["id"] for job in jobs],),
        )
        assert rows == [{"status": "completed", "attempt_count": 1, "stage": "completed"}] * 2
        assert (
            await database.fetch_one(
                "SELECT count(*) AS n FROM agent_worker_slots WHERE pool_key=%s AND status='held'",
                (pool,),
            )
        )["n"] == 0
        assert (
            await database.fetch_one(
                "SELECT status FROM agent_worker_instances WHERE id=%s",
                (worker.registration["id"],),
            )
        )["status"] == "stopped"

    asyncio.run(run())


def test_native_worker_shutdown_cancels_owned_work_and_releases_capacity(worker_jobs):
    database, pool, jobs = worker_jobs

    async def run():
        stop, entered, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class WaitingRuntime:
            async def execute(self, input, *, fence):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        worker = GenerationWorker(database, WaitingRuntime(), pool=pool)
        task = asyncio.create_task(worker.run(stop=stop))
        await asyncio.wait_for(entered.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 5)
        assert cancelled.is_set()
        assert (
            await database.fetch_one(
                "SELECT count(*) AS n FROM agent_worker_slots WHERE pool_key=%s AND status='held'",
                (pool,),
            )
        )["n"] == 0
        assert (
            await database.fetch_one(
                "SELECT status FROM agent_worker_instances WHERE id=%s",
                (worker.registration["id"],),
            )
        )["status"] == "stopped"
        assert (await Dispatch(database).get(jobs[0]["project_id"], jobs[0]["request_id"]))[
            "status"
        ] != "completed"

    asyncio.run(run())


@pytest.mark.parametrize("raises", [False, True])
def test_worker_never_labels_an_incomplete_handler_as_completed(worker_jobs, raises):
    database, pool, jobs = worker_jobs

    async def run():
        class IncompleteRuntime:
            async def execute(self, input, *, fence):
                if raises:
                    raise RuntimeError("private provider response must not enter public failure")

        worker = GenerationWorker(database, IncompleteRuntime(), pool=pool)
        assert await worker.execute_job(jobs[0], "fixture-worker")
        row = await Dispatch(database).get(jobs[0]["project_id"], jobs[0]["request_id"])
        assert row["status"] == "failed" and row["error_code"] == "GENERATION_WORKER_FAILED"
        assert "private provider response" not in row["error_message"]
        assert (
            await database.fetch_one(
                "SELECT status FROM user_requests WHERE id=%s", (jobs[0]["request_id"],)
            )
        )["status"] == "failed"

    asyncio.run(run())


def test_cancelled_dispatch_interrupts_running_domain_handler(worker_jobs):
    database, pool, jobs = worker_jobs

    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class WaitingRuntime:
            async def execute(self, input, *, fence):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        worker = GenerationWorker(
            database, WaitingRuntime(), pool=pool, ttl_ms=1000, heartbeat_ms=50
        )
        task = asyncio.create_task(worker.execute_job(jobs[0], "cancelled-worker"))
        await asyncio.wait_for(entered.wait(), 3)
        await UserRequests(database).cancel(jobs[0]["project_id"], jobs[0]["request_id"])
        assert await asyncio.wait_for(task, 3)
        assert cancelled.is_set()
        assert (await Dispatch(database).get(jobs[0]["project_id"], jobs[0]["request_id"]))[
            "status"
        ] == "cancelled"

    asyncio.run(run())


def test_durable_agent_usage_is_repaired_once_after_a_lost_accounting_write(worker_jobs, actor):
    database, _, jobs = worker_jobs

    async def run():
        input = {
            "id": "usage-backfill-" + uuid4().hex,
            "projectId": jobs[0]["project_id"],
            "requestId": jobs[0]["request_id"],
            "workspaceKey": "sha256:" + uuid4().hex + uuid4().hex,
            "provider": "openai",
            "model": "fixture",
            "frameworkVersion": "python-1",
            "buildRevision": "test",
            **{
                key: "a" * 64
                for key in ("profileHash", "promptHash", "toolHash", "skillHash", "workspaceHash")
            },
        }
        store = RuntimeStore(database)
        row = await store.create(input, owner="usage-worker")
        await store.complete(
            fence_for(row),
            "completed",
            1,
            {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        )
        assert (await reconcile_run_usage(database))["reconciled"] == 1
        assert (await reconcile_run_usage(database))["reconciled"] == 0
        rows = await database.fetch_all(
            "SELECT quantity FROM usage_events WHERE source_type='agent_run' AND source_id=%s",
            (input["id"],),
        )
        assert rows == [{"quantity": 15}]

    asyncio.run(run())
