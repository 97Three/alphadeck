import asyncio
from uuid import uuid4

import httpx
import psycopg
import pytest

from alphadeck_platform.admin import protect_last_admin
from alphadeck_platform.allocation import reconcile_projects
from alphadeck_platform.auth import Authorization
from alphadeck_platform.bootstrap import bootstrap
from alphadeck_platform.database import Database
from alphadeck_platform.quota import Quota
from app import create_app


@pytest.fixture
def peer(database_url):
    identity = str(uuid4())
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO auth_users (id,email,name,role,updated_at) VALUES (%s,%s,'Policy test peer','member',now())",
            (identity, identity + "@example.test"),
        )
    yield identity
    with psycopg.connect(database_url) as connection:
        connection.execute("DELETE FROM auth_users WHERE id=%s", (identity,))


def test_access_catalogue_atomic_versioned_overrides_and_quota_semantics(
    database_url, actor, peer, tmp_path
):
    database = Database(database_url)
    app = create_app(
        database, Authorization(database, enabled=True, secret="test-secret"), tmp_path
    )
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:3100",
            cookies={"alphadeck.session_token": actor["cookie"]},
        ) as client:
            result = await client.get("/api/admin/access-control")
            assert result.status_code == 200, result.text
            assert result.json()["data"]["version"] == 1
            target = f"/api/admin/users/{peer}/access"
            initial = await client.get(target)
            assert initial.status_code == 200, initial.text
            assert initial.json()["data"]["user"]["accessVersion"] == 0
            body = {
                "expectedAccessVersion": 0,
                "reason": "migration test",
                "permissionOverrides": [{"permissionKey": "project.create", "effect": "deny"}],
                "quotaOverrides": [
                    {
                        "metric": "projects.owned",
                        "limit": "3",
                        "enforcement": "hard",
                        "windowType": "lifetime",
                        "reservationTtlSeconds": 3600,
                    }
                ],
            }
            result = await client.patch(target, json=body)
            assert result.status_code == 200, result.text
            data = result.json()["data"]
            assert data["user"]["accessVersion"] == 1
            assert not next(
                item for item in data["permissions"] if item["action"] == "project.create"
            )["allowed"]
            assert (
                next(item for item in data["quotas"] if item["metric"] == "projects.owned")["limit"]
                == "3"
            )
            stale = await client.patch(target, json=body)
            assert stale.status_code == 409
            assert stale.json()["error"] == "ACCESS_POLICY_VERSION_CONFLICT"
            bad = await client.patch(
                target,
                json={
                    **body,
                    "expectedAccessVersion": 1,
                    "quotaOverrides": [{**body["quotaOverrides"][0], "enforcement": "observe"}],
                },
            )
            assert bad.status_code == 400
            assert bad.json()["error"] == "STRUCTURAL_QUOTA_MUST_BE_HARD"
            assert (await client.get(target)).json()["data"]["user"]["accessVersion"] == 1
            admin = await client.patch(
                f"/api/admin/users/{actor['user_id']}/access",
                json={**body, "expectedAccessVersion": 0},
            )
            assert admin.status_code == 409 and admin.json()["error"] == "ADMIN_POLICY_IS_UNLIMITED"
            usage = await client.get("/api/account/usage")
            assert all(item["unlimited"] for item in usage.json()["data"]["quotas"])
            audit = await client.get(
                "/api/admin/audit", params={"eventType": "admin.access_policy"}
            )
            assert any(item["targetId"] == peer for item in audit.json()["data"])

    asyncio.run(run())


def test_project_members_require_owner_and_cannot_remove_owner(database_url, actor, peer, tmp_path):
    identity = "members-" + str(uuid4())
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Membership test',%s,now())",
            (identity, actor["user_id"]),
        )
    database = Database(database_url)
    app = create_app(
        database, Authorization(database, enabled=True, secret="test-secret"), tmp_path
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"alphadeck.session_token": actor["cookie"]},
        ) as client:
            path = f"/api/projects/{identity}/members"
            added = await client.put(path, json={"email": peer + "@example.test", "role": "viewer"})
            assert added.status_code == 200, added.text
            assert added.json()["data"]["userId"] == peer
            listed = (await client.get(path)).json()["data"]
            assert listed["ownerId"] == actor["user_id"] and len(listed["memberships"]) == 1
            assert (
                await client.request("DELETE", path, json={"userId": actor["user_id"]})
            ).status_code == 400
            removed = await client.request("DELETE", path, json={"userId": peer})
            assert removed.json()["data"]["removedCount"] == 1
            with psycopg.connect(database_url) as connection:
                connection.execute("UPDATE projects SET owner_id=%s WHERE id=%s", (peer, identity))
                connection.execute(
                    "INSERT INTO project_memberships (id,project_id,user_id,role,updated_at) VALUES (%s,%s,%s,'editor',now())",
                    (str(uuid4()), identity, actor["user_id"]),
                )
            assert (await client.get(path)).status_code == 403

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM projects WHERE id=%s", (identity,))


def test_allocation_repair_skips_inflight_creation_and_repairs_once(database_url, actor):
    database = Database(database_url)

    async def run():
        quota = Quota(database)
        reservation = await quota.reserve(actor["user_id"], "projects.owned", 1, str(uuid4()))
        async with database.transaction() as connection:
            inflight = await reconcile_projects(connection, actor["user_id"])
        assert inflight["status"] == "skipped-active-reservations"
        await quota.release(reservation["reservation"]["id"])
        with psycopg.connect(database_url) as connection:
            connection.execute(
                "UPDATE usage_buckets SET used=2 WHERE actor_user_id=%s", (actor["user_id"],)
            )
        async with database.transaction() as connection:
            changed = await reconcile_projects(connection, actor["user_id"])
        assert changed["status"] == "reconciled" and changed["adjustment"] == -2
        async with database.transaction() as connection:
            same = await reconcile_projects(connection, actor["user_id"])
        assert same["status"] == "unchanged"
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM usage_events WHERE actor_user_id=%s AND source_type='allocation_reconciliation'",
                    (actor["user_id"],),
                ).fetchone()[0]
                == 1
            )

    asyncio.run(run())


def test_bootstrap_explicit_credentials_revoke_previous_sessions(database_url, actor, monkeypatch):
    monkeypatch.setenv("ALPHADECK_AUTH_ADMIN_EMAIL", actor["user_id"] + "@example.test")
    monkeypatch.setenv("ALPHADECK_AUTH_ADMIN_PASSWORD", "Bootstrap-password-42!")
    monkeypatch.setenv("NODE_ENV", "production")
    database = Database(database_url)
    result = asyncio.run(
        bootstrap(database, Authorization(database, enabled=True, secret="test-secret"))
    )
    assert result["userId"] == actor["user_id"] and not result["usedDevelopmentDefaults"]
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT role,must_change_password,permission_profile_id,quota_profile_id FROM auth_users WHERE id=%s",
            (actor["user_id"],),
        ).fetchone() == ("admin", False, None, None)
        assert (
            connection.execute(
                "SELECT count(*) FROM auth_sessions WHERE user_id=%s", (actor["user_id"],)
            ).fetchone()[0]
            == 0
        )


def test_concurrent_admin_removal_always_keeps_one_active_admin(database_url, actor, peer):
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE auth_users SET role='admin' WHERE id=ANY(%s)", ([actor["user_id"], peer],)
        )
        others = connection.execute(
            "SELECT id FROM auth_users WHERE role='admin' AND id<>ALL(%s)",
            ([actor["user_id"], peer],),
        ).fetchall()
        assert not others, "Test requires isolated administrators"
    database = Database(database_url)

    async def remove(identity):
        try:
            async with database.transaction() as connection:
                await protect_last_admin(connection, identity)
                await connection.execute(
                    "UPDATE auth_users SET role='member' WHERE id=%s", (identity,)
                )
            return True
        except ValueError:
            return False

    async def run():
        return await asyncio.gather(remove(actor["user_id"]), remove(peer))

    assert sorted(asyncio.run(run())) == [False, True]
