import asyncio
from uuid import uuid4

import httpx
import psycopg
import pytest

from alphadeck_platform.auth import Authorization, AuthorizationError
from alphadeck_platform.database import Database
from app import create_app


def test_health_requires_no_database():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            result = await client.get("/api/health")
            assert result.json() == {"ok": True, "service": "alphadeck-web"}
            assert result.headers["cache-control"] == "no-store, max-age=0"

    asyncio.run(run())


def test_disabled_auth_rejects_session_management(tmp_path):
    app = create_app(object(), Authorization(None, enabled=False, secret=""), tmp_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/account/sessions")
            assert response.status_code == 401
            assert response.json()["error"] == "AUTH_DISABLED"

    asyncio.run(run())


def test_settings_permissions_and_serialization(database_url, actor, tmp_path):
    database = Database(database_url)
    app = create_app(
        database, Authorization(database, enabled=True, secret="test-secret"), tmp_path
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/settings/global")).status_code == 401
            client.cookies.set("alphadeck.session_token", actor["cookie"])
            response = await client.get("/api/settings/global")
            assert response.status_code == 200, response.text
            assert response.json()["cliSettings"] == response.json()["cli_settings"]
            assert response.headers["cache-control"] == "private, no-store"
            assert (
                await client.put(
                    "/api/settings/global",
                    json={"cliSettings": {"pi": {"model": "deepseek direct"}}},
                )
            ).status_code == 403
            with psycopg.connect(database_url) as connection:
                connection.execute(
                    "UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],)
                )
            response = await client.put(
                "/api/settings", json={"cliSettings": {"pi": {"model": "deepseek direct"}}}
            )
            assert response.status_code == 200
            assert response.json()["cli_settings"]["pi"]["model"] == "deepseek-v4-flash"
            assert (await client.get("/api/settings/global")).json() == response.json()
            with psycopg.connect(database_url) as connection:
                connection.execute(
                    "UPDATE auth_users SET banned=true WHERE id=%s", (actor["user_id"],)
                )
            assert (await client.get("/api/settings/global")).status_code == 403

    asyncio.run(run())


def test_revoking_sessions_is_scoped_and_audited(database_url, actor, tmp_path):
    database = Database(database_url)
    app = create_app(
        database, Authorization(database, enabled=True, secret="test-secret"), tmp_path
    )
    other_id = str(uuid4())
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO auth_sessions (id,token,user_id,expires_at,updated_at)
               VALUES (%s,%s,%s,now()+interval '1 hour',now())""",
            (other_id, str(uuid4()), actor["user_id"]),
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("alphadeck.session_token", actor["cookie"])
            sessions = (await client.get("/api/account/sessions")).json()["data"]
            assert len(sessions) == 2
            assert sum(row["isCurrent"] for row in sessions) == 1
            assert all("token" not in row for row in sessions)
            assert (
                await client.request(
                    "DELETE",
                    "/api/account/sessions",
                    json={"allOthers": True, "sessionId": other_id},
                )
            ).status_code == 400
            response = await client.request(
                "DELETE", "/api/account/sessions", json={"allOthers": True}
            )
            assert response.json() == {"success": True, "data": {"revokedCount": 1}}
            assert len((await client.get("/api/account/sessions")).json()["data"]) == 1
            with psycopg.connect(database_url) as connection:
                audit = connection.execute(
                    "SELECT metadata FROM auth_audit_events WHERE actor_user_id=%s AND event_type='session.revoked'",
                    (actor["user_id"],),
                ).fetchone()
                assert audit[0]["revokedCount"] == 1

    asyncio.run(run())


def test_database_membership_and_role_intersection(database_url, actor):
    from starlette.requests import Request

    database = Database(database_url)
    authorization = Authorization(database, enabled=True, secret="test-secret")
    project_id = "migration-" + str(uuid4())
    request = Request(
        {
            "type": "http",
            "headers": [(b"cookie", f"alphadeck.session_token={actor['cookie']}".encode())],
        }
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Migration test',%s,now())",
            (project_id, actor["user_id"]),
        )

    async def run():
        owner = await authorization.require_action(request, "project.source.write", project_id)
        assert owner.project_role == "owner"
        with psycopg.connect(database_url) as connection:
            connection.execute("UPDATE projects SET owner_id=NULL WHERE id=%s", (project_id,))
            connection.execute(
                "INSERT INTO project_memberships (id,project_id,user_id,role,updated_at) VALUES (%s,%s,%s,'viewer',now())",
                (str(uuid4()), project_id, actor["user_id"]),
            )
        assert (
            await authorization.require_action(request, "project.source.read", project_id)
        ).project_role == "viewer"
        with pytest.raises(AuthorizationError) as denied:
            await authorization.require_action(request, "project.source.write", project_id)
        assert denied.value.status == 403
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM project_memberships WHERE project_id=%s", (project_id,))
        with pytest.raises(AuthorizationError) as missing:
            await authorization.require_action(request, "project.read", project_id)
        assert missing.value.status == 404

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM projects WHERE id=%s", (project_id,))
