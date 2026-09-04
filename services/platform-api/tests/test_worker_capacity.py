import asyncio
from uuid import uuid4

import pytest

from alphadeck_platform.database import Database
from alphadeck_platform.lease_session import LeaseSession
from alphadeck_platform.worker_capacity import WorkerCapacity, WorkerError, WorkerRegistry


@pytest.fixture
def worker_pool(database_url):
    database = Database(database_url)
    pool = "test." + uuid4().hex
    yield database, pool

    async def cleanup():
        async with database.transaction() as tx:
            await tx.execute("DELETE FROM agent_worker_slots WHERE pool_key=%s", (pool,))
            await tx.execute("DELETE FROM agent_worker_instances WHERE pool_key=%s", (pool,))

    asyncio.run(cleanup())


def test_worker_pool_configuration_consistency_and_expired_registration(worker_pool):
    database, pool = worker_pool
    registry = WorkerRegistry(database)

    async def run():
        first = await registry.register(1, 2, 60000, pool=pool)
        assert first["processConcurrency"] == 1 and first["globalConcurrency"] == 2
        second = await registry.register(2, 2, 60000, pool=pool)
        with pytest.raises(WorkerError) as error:
            await registry.register(1, 3, 60000, pool=pool)
        assert error.value.code == "WORKER_POOL_CONFIG_MISMATCH"
        await registry.heartbeat(first, 60000)
        async with database.transaction() as tx:
            await tx.execute(
                "UPDATE agent_worker_instances SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
                (first["id"],),
            )
        with pytest.raises(WorkerError) as error:
            await registry.heartbeat(first, 60000)
        assert error.value.code == "WORKER_INSTANCE_LEASE_LOST"
        await registry.stop(second)
        third = await registry.register(1, 3, 60000, pool=pool)
        assert third["globalConcurrency"] == 3
        assert (
            await database.fetch_one(
                "SELECT status FROM agent_worker_instances WHERE id=%s", (first["id"],)
            )
        )["status"] == "stale"
        await registry.stop(third)

    asyncio.run(run())


def test_global_capacity_parallel_claims_and_stale_owner_fences(worker_pool):
    database, pool = worker_pool
    capacity = WorkerCapacity(database)

    async def run():
        claims = await asyncio.gather(
            *(
                capacity.claim(2, str(uuid4()), "worker-" + str(index), 60000, pool=pool)
                for index in range(8)
            )
        )
        claims = [claim for claim in claims if claim]
        assert len(claims) == 2 and {claim["slotNumber"] for claim in claims} == {1, 2}
        await capacity.heartbeat(claims[0], 60000)
        await capacity.release(claims[0])
        replacement = await capacity.claim(2, str(uuid4()), "replacement", 60000, pool=pool)
        assert replacement["slotNumber"] == claims[0]["slotNumber"]
        assert replacement["fencingToken"] > claims[0]["fencingToken"]
        for operation in (capacity.heartbeat(claims[0], 60000), capacity.release(claims[0])):
            with pytest.raises(WorkerError) as error:
                await operation
            assert error.value.code == "WORKER_SLOT_LEASE_LOST"
        await capacity.release(replacement)
        await capacity.release(claims[1])
        job = str(uuid4())
        first = await capacity.claim(2, job, "one", 60000, pool=pool)
        assert await capacity.claim(2, job, "two", 60000, pool=pool) is None
        await capacity.release(first)

    asyncio.run(run())


def test_live_generation_job_protects_expired_global_slot(worker_pool, database_url, actor):
    import psycopg

    from alphadeck_platform.dispatch import Dispatch
    from alphadeck_platform.user_requests import UserRequests

    database, pool = worker_pool
    project, request = "slot-" + uuid4().hex, str(uuid4())
    with psycopg.connect(database_url) as tx:
        tx.execute(
            "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Slot contract',%s,now())",
            (project, actor["user_id"]),
        )

    async def run():
        await UserRequests(database).create(request, project, "test", actor=actor["user_id"])
        job = await Dispatch(database).enqueue(project, request, "test")
        await Dispatch(database).claim(project, request, "dispatch-worker", 60000)
        slots = WorkerCapacity(database)
        claim = await slots.claim(1, str(job["id"]), "slot-owner", 60000, pool=pool)
        async with database.transaction() as tx:
            await tx.execute(
                "UPDATE agent_worker_slots SET lease_expires_at=now()-interval '1 second' WHERE pool_key=%s",
                (pool,),
            )
        assert await slots.claim(1, str(uuid4()), "replacement", 60000, pool=pool) is None
        with pytest.raises(WorkerError):
            await slots.heartbeat(claim, 60000)
        await Dispatch(database).cancel(project, request)
        replacement = await slots.claim(1, str(uuid4()), "replacement", 60000, pool=pool)
        assert replacement and replacement["fencingToken"] > claim["fencingToken"]
        await slots.release(replacement)

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as tx:
            tx.execute("DELETE FROM projects WHERE id=%s", (project,))


def test_lease_session_cancels_operation_when_database_renewal_fails():
    async def run():
        cancelled, released = asyncio.Event(), asyncio.Event()

        async def heartbeat():
            raise WorkerError("WORKER_SLOT_LEASE_LOST", "fixture lease lost")

        async def release():
            released.set()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with pytest.raises(WorkerError):
            async with LeaseSession(
                heartbeat, ttl_ms=100, interval_ms=10, release=release
            ) as lease:
                await lease.run(operation)
        assert cancelled.is_set() and released.is_set()
        with pytest.raises(ValueError):
            LeaseSession(heartbeat, ttl_ms=100, interval_ms=100)

    asyncio.run(run())
