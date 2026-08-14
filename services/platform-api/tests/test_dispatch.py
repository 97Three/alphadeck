import asyncio
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from alphadeck_platform.database import Database
from alphadeck_platform.dispatch import Dispatch, DispatchError
from alphadeck_platform.generation_lease import GenerationLease, GenerationLeaseError
from alphadeck_platform.quota import QuotaError


@pytest.fixture
def requests(database_url, actor):
    projects = ["dispatch-" + str(uuid4()) for _ in range(2)]
    identities = [str(uuid4()) for _ in range(3)]
    with psycopg.connect(database_url) as connection:
        for project in projects:
            connection.execute(
                "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Dispatch contract',%s,now())",
                (project, actor["user_id"]),
            )
        for request, project in zip(
            identities, [projects[0], projects[0], projects[1]], strict=True
        ):
            connection.execute(
                "INSERT INTO user_requests (id,project_id,actor_user_id,instruction) VALUES (%s,%s,%s,'Test instruction')",
                (request, project, actor["user_id"]),
            )
    yield {"projects": projects, "ids": identities, "database": Database(database_url)}
    with psycopg.connect(database_url) as connection:
        connection.execute("DELETE FROM projects WHERE id=ANY(%s)", (projects,))


def test_queue_idempotency_outbox_order_and_cancellation_fences(requests):
    store = Dispatch(requests["database"])
    project = requests["projects"][0]
    request = requests["ids"][0]

    async def run():
        rows = await asyncio.gather(
            *(
                store.enqueue(project, request, "Test instruction", stage="planning_data_prefetch")
                for _ in range(2)
            )
        )
        assert rows[0]["id"] == rows[1]["id"]
        with pytest.raises(DispatchError, match="different instruction"):
            await store.enqueue(project, request, "Other instruction")
        with pytest.raises(DispatchError) as sensitive:
            await store.enqueue(
                project,
                request,
                "Test instruction",
                execution_envelope={"nested": {"api_key": "do not persist"}},
            )
        assert sensitive.value.code == "GENERATION_DISPATCH_SENSITIVE_ENVELOPE"
        claim = await store.claim(project, request, "first-worker", 60000)
        with pytest.raises(DispatchError) as duplicate:
            await store.claim(project, request, "second-worker", 60000)
        assert duplicate.value.code == "GENERATION_DISPATCH_BUSY"
        await store.checkpoint(claim, {"schemaVersion": 1, "prepared": True})
        current = await store.read_claimed(claim)
        assert current["execution_envelope"]["prepared"] and current["max_attempts"] == 3
        await store.heartbeat(claim, 120000)
        await store.cancel(project, request)
        with pytest.raises(DispatchError) as stale:
            await store.heartbeat(claim, 60000)
        assert stale.value.code == "GENERATION_DISPATCH_LEASE_LOST"
        assert (await store.finish(project, request, "completed", fence=claim))[
            "status"
        ] == "cancelled"
        events = await store.pending_events(project)
        assert [row["sequence"] for row in events] == [1, 2, 3, 4]
        assert [row["event_type"] for row in events] == [
            "generation_queued",
            "generation_claimed",
            "generation_preparation_completed",
            "generation_cancelled",
        ]
        assert await store.mark_published([row["id"] for row in events]) == 4
        assert await store.mark_published([row["id"] for row in events]) == 0

    asyncio.run(run())


def test_only_one_job_runs_per_project_and_actor_capacity_is_serialized(
    database_url, actor, requests
):
    store = Dispatch(requests["database"])
    p1, p2 = requests["projects"]
    r1, r2, r3 = requests["ids"]
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO user_quota_overrides (id,user_id,metric,"limit",enforcement,window_type,reservation_ttl_seconds,updated_at)
            VALUES (%s,%s,'agent.concurrent',1,'hard','lifetime',3600,now())""",
            (str(uuid4()), actor["user_id"]),
        )

    async def run():
        for project, request in ((p1, r1), (p1, r2), (p2, r3)):
            await store.enqueue(project, request, "Test instruction")
        outcomes = await asyncio.gather(
            store.claim(p1, r1, "worker-a", 60000),
            store.claim(p2, r3, "worker-b", 60000),
            return_exceptions=True,
        )
        assert sum(isinstance(item, QuotaError) for item in outcomes) == 1
        claim = next(item for item in outcomes if isinstance(item, dict))
        with pytest.raises(DispatchError) as no_fence:
            await store.finish(claim["projectId"], claim["requestId"], "completed")
        assert no_fence.value.code == "GENERATION_DISPATCH_FENCE_REQUIRED"
        if claim["projectId"] == p1:
            with pytest.raises(DispatchError) as busy:
                await store.claim(p1, r2, "worker-c", 60000)
            assert busy.value.code == "GENERATION_PROJECT_BUSY"
        await store.finish(claim["projectId"], claim["requestId"], "completed", fence=claim)
        replay = await store.finish(claim["projectId"], claim["requestId"], "completed")
        assert replay["status"] == "completed"
        with pytest.raises(DispatchError) as conflict:
            await store.finish(claim["projectId"], claim["requestId"], "failed", fence=claim)
        assert conflict.value.code == "GENERATION_DISPATCH_TERMINAL_CONFLICT"

    asyncio.run(run())


def test_expired_worker_replans_only_after_finer_generation_lease_ends(
    database_url, requests, monkeypatch
):
    store = Dispatch(requests["database"])
    lease = GenerationLease(requests["database"])
    project, request = requests["projects"][0], requests["ids"][0]
    monkeypatch.setenv("PI_AGENT_DISPATCH_MODE", "worker")
    monkeypatch.setenv("PI_AGENT_DISPATCH_RETRY_BASE_DELAY_MS", "100")

    async def run():
        await store.enqueue(project, request, "Test instruction", max_attempts=2)
        old = await store.claim(project, request, "lost-worker", 60000)
        finer = await lease.claim(project, "inner-operation", "inner-owner", 60000, request=request)
        with psycopg.connect(database_url) as connection:
            connection.execute(
                "UPDATE agent_generation_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE project_id=%s",
                (project,),
            )
        assert await store.reconcile(project=project) == []
        await lease.release(finer)
        changed = await store.reconcile(project=project)
        assert len(changed) == 1 and changed[0]["status"] == "retry_wait"
        with pytest.raises(DispatchError) as stale:
            await store.finish(project, request, "completed", fence=old)
        assert stale.value.code == "GENERATION_DISPATCH_LEASE_LOST"
        with psycopg.connect(database_url) as connection:
            connection.execute(
                "UPDATE agent_generation_jobs SET available_at=clock_timestamp()-interval '1 second' WHERE project_id=%s",
                (project,),
            )
        new = await store.claim(project, request, "replacement-worker", 60000)
        assert new["fencingToken"] > old["fencingToken"] and new["attemptCount"] == 2
        with psycopg.connect(database_url) as connection:
            connection.execute(
                "UPDATE agent_generation_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE project_id=%s",
                (project,),
            )
        changed = await store.reconcile(project=project)
        assert changed[0]["status"] == "interrupted"
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM user_requests WHERE id=%s", (request,)
                ).fetchone()[0]
                == "failed"
            )

    asyncio.run(run())


def test_project_lease_rejects_old_fences_and_rolls_back_expired_write(database_url, requests):
    lease = GenerationLease(requests["database"])
    project, request = requests["projects"][0], requests["ids"][0]

    async def run():
        old = await lease.claim(project, "one", "owner-1", 60000, request=request)
        with pytest.raises(GenerationLeaseError) as busy:
            await lease.claim(project, "two", "owner-2", 60000, request=request)
        assert busy.value.code == "GENERATION_PROJECT_BUSY"
        with psycopg.connect(database_url) as connection:
            connection.execute(
                "UPDATE agent_generation_leases SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE project_id=%s",
                (project,),
            )
        new = await lease.claim(project, "two", "owner-2", 60000, request=request)
        with pytest.raises(GenerationLeaseError) as stale:
            await lease.release(old)
        assert stale.value.code == "GENERATION_LEASE_LOST"

        async def change_then_expire(connection):
            await connection.execute(
                "UPDATE projects SET name='should roll back' WHERE id=%s", (project,)
            )
            await connection.execute(
                "UPDATE agent_generation_leases SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE project_id=%s",
                (project,),
            )

        with pytest.raises(GenerationLeaseError) as expired:
            await lease.fenced(new, change_then_expire)
        assert expired.value.code == "GENERATION_LEASE_EXPIRED"
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute("SELECT name FROM projects WHERE id=%s", (project,)).fetchone()[
                    0
                ]
                == "Dispatch contract"
            )
        await lease.heartbeat(new, 60000)
        await lease.release(new)

    asyncio.run(run())


def test_outbox_failure_rolls_back_job_claim(database_url, requests):
    store = Dispatch(requests["database"])
    project, request = requests["projects"][0], requests["ids"][0]
    asyncio.run(store.enqueue(project, request, "Test instruction"))
    name = "dispatch_failure_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        connection.execute(
            sql.SQL(
                "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.project_id={} AND NEW.event_type='generation_claimed' THEN RAISE EXCEPTION 'intentional outbox failure'; END IF; RETURN NEW; END; $$"
            ).format(sql.Identifier(name), sql.Literal(project))
        )
        connection.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT ON agent_generation_outbox_events FOR EACH ROW EXECUTE FUNCTION {}()"
            ).format(sql.Identifier(name), sql.Identifier(name))
        )
    try:
        with pytest.raises(psycopg.Error, match="intentional outbox failure"):
            asyncio.run(store.claim(project, request, "worker", 60000))
        current = asyncio.run(store.get(project, request))
        assert (
            current["status"] == "pending"
            and current["attempt_count"] == 0
            and current["event_sequence"] == 1
        )
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute(
                sql.SQL("DROP TRIGGER {} ON agent_generation_outbox_events").format(
                    sql.Identifier(name)
                )
            )
            connection.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(name)))
