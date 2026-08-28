import asyncio
from uuid import uuid4

import httpx
import psycopg
import pytest

from alphadeck_platform.auth import Authorization
from alphadeck_platform.database import Database
from alphadeck_platform.dispatch import Dispatch
from alphadeck_platform.messages import insert_message, serialize_message
from alphadeck_platform.realtime import Realtime, sse
from alphadeck_platform.runtime_policy import RuntimeStoreError
from alphadeck_platform.runtime_store import RuntimeStore, fence_for
from alphadeck_platform.user_requests import RequestBindingError, UserRequests
from app import create_app


@pytest.fixture
def request_context(database_url, actor):
    projects = ["request-" + str(uuid4()) for _ in range(2)]
    with psycopg.connect(database_url) as tx:
        for project in projects:
            tx.execute(
                "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Request contract',%s,now())",
                (project, actor["user_id"]),
            )
    yield Database(database_url), projects
    with psycopg.connect(database_url) as tx:
        tx.execute("DELETE FROM projects WHERE id=ANY(%s)", (projects,))


def test_request_claim_races_binding_and_terminal_immutability(request_context, actor):
    database, projects = request_context
    store = UserRequests(database)
    identity = str(uuid4())

    async def run():
        outcomes = await asyncio.gather(
            *(
                store.create(identity, projects[0], "original", actor=actor["user_id"], cli="pi")
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
        error = next(outcome for outcome in outcomes if isinstance(outcome, RequestBindingError))
        assert error.code == "REQUEST_ALREADY_EXISTS" and error.status == 409
        with pytest.raises(RequestBindingError) as conflict:
            await store.create(identity, projects[1], "wrong", actor=actor["user_id"], upsert=True)
        assert conflict.value.code == "REQUEST_PROJECT_MISMATCH"
        with pytest.raises(RequestBindingError) as conflict:
            await store.binding(projects[0], identity, None)
        assert conflict.value.code == "REQUEST_ACTOR_MISMATCH"
        row = await store.create(
            identity, projects[0], "updated", actor=actor["user_id"], upsert=True
        )
        assert row["instruction"] == "updated" and row["cli_preference"] == "pi"
        assert await store.status(projects[0], identity, "running")
        assert await store.status(projects[0], identity, "completed")
        assert not await store.status(projects[0], identity, "running")
        assert not await store.status(projects[0], identity, "failed", "late failure")
        assert (
            await database.fetch_one(
                "SELECT status,error_message FROM user_requests WHERE id=%s", (identity,)
            )
        ) == {"status": "completed", "error_message": None}

    asyncio.run(run())


def test_request_expiration_respects_cross_process_database_leases(request_context, actor):
    database, projects = request_context
    store = UserRequests(database)
    old, live = str(uuid4()), str(uuid4())

    async def run():
        for identity in (old, live):
            await store.create(identity, projects[0], "test", actor=actor["user_id"])
        await Dispatch(database).enqueue(projects[0], live, "test")
        async with database.transaction() as tx:
            await tx.execute(
                "UPDATE user_requests SET created_at=now()-interval '1 hour' WHERE id=ANY(%s)",
                ([old, live],),
            )
        assert await store.active(projects[0]) == {"hasActiveRequests": True, "activeCount": 1}
        rows = await database.fetch_all(
            "SELECT id,status FROM user_requests WHERE id=ANY(%s)", ([old, live],)
        )
        assert {row["id"]: row["status"] for row in rows} == {old: "failed", live: "pending"}

    asyncio.run(run())


def test_pause_route_fences_old_worker_and_does_not_touch_another_project(request_context, actor):
    database, projects = request_context
    store = UserRequests(database)
    identities = [str(uuid4()), str(uuid4())]

    async def run():
        for identity, project in zip(identities, projects, strict=True):
            await store.create(identity, project, "test", actor=actor["user_id"])
        job = await Dispatch(database).enqueue(projects[0], identities[0], "test")
        input = {
            "id": "cancel-run-" + str(uuid4()),
            "projectId": projects[0],
            "requestId": identities[0],
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
        runtime = RuntimeStore(database)
        row = await runtime.create(input, owner="old-worker")
        app = create_app(database, Authorization(database, enabled=True, secret="test-secret"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"alphadeck.session_token": actor["cookie"]},
        ) as client:
            response = await client.post(
                f"/api/chat/{projects[0]}/pause", json={"requestId": identities[1]}
            )
            assert response.status_code == 409
            response = await client.post(
                f"/api/chat/{projects[0]}/pause", json={"requestId": identities[0]}
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"] == {
                "cancelled": 1,
                "activeCount": 0,
                "cancelledMissions": 0,
            }
            assert (await client.get(f"/api/chat/{projects[0]}/requests/active")).json()[
                "activeCount"
            ] == 0
        assert await store.cancelled(projects[0], identities[0])
        assert not await store.cancelled(projects[1], identities[1])
        with pytest.raises(RuntimeStoreError):
            await runtime.heartbeat(fence_for(row), 60000)
        assert (
            await database.fetch_one(
                "SELECT status FROM agent_generation_jobs WHERE id=%s", (job["id"],)
            )
        )["status"] == "cancelled"
        assert await store.cancel(projects[0], identities[0]) == {
            "cancelled": 0,
            "activeCount": 0,
            "cancelledMissions": 0,
        }

    asyncio.run(run())


def test_realtime_cross_connection_delivery_and_large_message_project_binding(request_context):
    database, projects = request_context

    async def run():
        publisher, subscriber = Realtime(Database(database.url)), Realtime(database)
        async with subscriber.listen(projects[0]) as notifications:
            pending = asyncio.create_task(anext(notifications))
            await publisher.publish(
                projects[1], {"type": "status", "data": {"status": "other project"}}
            )
            expected = {"type": "status", "data": {"status": "generating", "requestId": "fixture"}}
            await publisher.publish(projects[0], expected)
            assert await asyncio.wait_for(pending, 3) == expected
            async with database.transaction() as tx:
                row = await insert_message(tx, projects[0], "assistant", "研究结果😀" * 5000)
            expected = {"type": "message", "data": serialize_message(row)}
            pending = asyncio.create_task(anext(notifications))
            await publisher.publish(projects[0], expected)
            assert await asyncio.wait_for(pending, 3) == expected
            assert (
                await subscriber.decode(
                    projects[1], '{"type":"message_ref","id":"' + row["id"] + '"}'
                )
                is None
            )
            with pytest.raises(ValueError):
                await publisher.publish(projects[1], expected)
            assert sse({"type": "status", "data": {"status": "中文\n😀"}}).startswith(b"data: ")

    asyncio.run(run())
