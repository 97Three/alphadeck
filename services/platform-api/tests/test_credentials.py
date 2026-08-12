import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

from alphadeck_platform.auth import Authorization, verify_cookie
from alphadeck_platform.credentials import AuthOptions
from alphadeck_platform.database import Database
from app import create_app

REFERENCE = json.loads((Path(__file__).parent / "fixtures/password.json").read_text())


def client_for(database_url, actor, tmp_path):
    database = Database(database_url)
    app = create_app(
        database, Authorization(database, enabled=True, secret="test-secret"), tmp_path
    )
    with psycopg.connect(database_url) as connection:
        connection.execute("DELETE FROM auth_rate_limits")
        connection.execute(
            "INSERT INTO auth_accounts (id,user_id,account_id,provider_id,password,updated_at) VALUES (%s,%s,%s,'credential',%s,now())",
            (str(uuid4()), actor["user_id"], actor["user_id"], REFERENCE["hash"]),
        )
    return app, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:3200"
    )


def test_password_hashes_are_compatible_with_original_node_argon2():
    async def run():
        options = AuthOptions()
        assert await options.verify(REFERENCE["hash"], REFERENCE["password"])
        assert not await options.verify(REFERENCE["hash"], "wrong")
        assert not await options.verify("corrupt", REFERENCE["password"])
        new_hash = await options.hash_password(REFERENCE["password"])
        assert new_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
        assert await options.verify(new_hash, REFERENCE["password"])

    asyncio.run(run())


def test_sign_in_session_cookie_csrf_and_sign_out(database_url, actor, tmp_path):
    _, client = client_for(database_url, actor, tmp_path)
    email = actor["user_id"] + "@example.test"

    async def run():
        async with client:
            body = {"email": email, "password": REFERENCE["password"], "rememberMe": False}
            blocked = await client.post(
                "/api/auth/sign-in/email", json=body, headers={"Origin": "https://attacker.test"}
            )
            assert blocked.status_code == 403
            assert (await client.get("/api/auth/get-session")).json() is None
            response = await client.post(
                "/api/auth/sign-in/email", json=body, headers={"Origin": "http://localhost:3200"}
            )
            assert response.status_code == 200, response.text
            token = response.json()["token"]
            assert (
                verify_cookie(client.cookies.get("alphadeck.session_token"), "test-secret")
                == token
            )
            assert (
                verify_cookie(client.cookies.get("alphadeck.dont_remember"), "test-secret")
                == "true"
            )
            assert "Max-Age" not in response.headers.get_list("set-cookie")[0]
            session = (await client.get("/api/auth/get-session")).json()
            assert session["user"]["id"] == actor["user_id"]
            assert session["session"]["token"] == token
            assert "credential_password" not in str(session)
            listed = await client.get("/api/account/sessions")
            assert all("token" not in value for value in listed.json()["data"])
            assert (await client.post("/api/auth/sign-out", json={})).json() == {"success": True}
            assert (await client.get("/api/auth/get-session")).json() is None
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT id FROM auth_sessions WHERE token=%s", (token,)
                ).fetchone()
                is None
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM auth_audit_events WHERE actor_user_id=%s AND event_type IN ('authentication.login_success','authentication.logout')",
                    (actor["user_id"],),
                ).fetchone()[0]
                == 2
            )

    asyncio.run(run())


def test_password_change_preserves_current_session_and_revokes_others(
    database_url, actor, tmp_path
):
    app, client = client_for(database_url, actor, tmp_path)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE auth_users SET must_change_password=true WHERE id=%s", (actor["user_id"],)
        )
        connection.execute(
            "INSERT INTO auth_sessions (id,user_id,token,expires_at,updated_at) VALUES (%s,%s,%s,now()+interval '1 hour',now())",
            (str(uuid4()), actor["user_id"], str(uuid4())),
        )
    client.cookies.set("alphadeck.session_token", actor["cookie"])

    async def run():
        async with client:
            assert (await client.get("/api/projects")).status_code == 428
            response = await client.post(
                "/api/account/password",
                json={
                    "currentPassword": REFERENCE["password"],
                    "newPassword": "New-password-43!",
                    "confirmPassword": "New-password-43!",
                },
            )
            assert response.status_code == 200, response.text
            session = (await client.get("/api/auth/get-session")).json()
            assert not session["user"]["mustChangePassword"]
            assert session["session"]["id"] == actor["session_id"]
            assert len((await client.get("/api/account/sessions")).json()["data"]) == 1
        with psycopg.connect(database_url) as connection:
            hashed = connection.execute(
                "SELECT password FROM auth_accounts WHERE user_id=%s", (actor["user_id"],)
            ).fetchone()[0]
        assert await app.state.credentials.options.verify(hashed, "New-password-43!")
        assert not await app.state.credentials.options.verify(hashed, REFERENCE["password"])

    asyncio.run(run())


def test_sign_in_limits_and_invalid_callback_are_rejected(database_url, actor, tmp_path):
    _, client = client_for(database_url, actor, tmp_path)
    body = {"email": actor["user_id"] + "@example.test", "password": REFERENCE["password"]}

    async def run():
        async with client:
            invalid = await client.post(
                "/api/auth/sign-in/email", json={**body, "callbackURL": "javascript:alert(1)"}
            )
            assert invalid.status_code == 403
            for _ in range(4):
                wrong = await client.post(
                    "/api/auth/sign-in/email", json={**body, "password": "wrong"}
                )
                assert wrong.status_code == 401
            limited = await client.post("/api/auth/sign-in/email", json=body)
            assert limited.status_code == 429
            assert int(limited.headers["x-retry-after"]) > 0

    asyncio.run(run())


def test_admin_creation_role_transition_password_reset_and_freshness(database_url, actor, tmp_path):
    _, client = client_for(database_url, actor, tmp_path)
    client.cookies.set("alphadeck.session_token", actor["cookie"])
    child = None

    async def run():
        nonlocal child
        async with client:
            assert (await client.get("/api/admin/users")).status_code == 403
            with psycopg.connect(database_url) as connection:
                connection.execute(
                    "UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],)
                )
            response = await client.post(
                "/api/admin/users", json={"name": "Child", "email": str(uuid4()) + "@example.test"}
            )
            assert response.status_code == 201, response.text
            child = response.json()["data"]["userId"]
            initial = response.json()["data"]["initialPassword"]
            assert len(initial) >= 12
            with psycopg.connect(database_url) as connection:
                user = connection.execute(
                    "SELECT must_change_password,permission_profile_id,quota_profile_id FROM auth_users WHERE id=%s",
                    (child,),
                ).fetchone()
                assert user[0] and user[1] and user[2]
            response = await client.patch(
                "/api/admin/users", json={"action": "set-role", "userId": child, "role": "admin"}
            )
            assert response.status_code == 200, response.text
            with psycopg.connect(database_url) as connection:
                assert connection.execute(
                    "SELECT role,permission_profile_id,quota_profile_id,access_version FROM auth_users WHERE id=%s",
                    (child,),
                ).fetchone() == ("admin", None, None, 1)
            response = await client.patch(
                "/api/admin/users", json={"action": "reset-password", "userId": child}
            )
            assert response.status_code == 200
            assert response.json()["data"]["initialPassword"] != initial
            assert (
                await client.patch(
                    "/api/admin/users",
                    json={"action": "set-status", "userId": actor["user_id"], "banned": True},
                )
            ).status_code == 400
            listed = await client.get("/api/admin/users", params={"q": "Child"})
            assert listed.json()["data"]["total"] == 1
            assert "password" not in listed.text.lower().replace("mustchangepassword", "").replace(
                "passwordchangedat", ""
            )
            with psycopg.connect(database_url) as connection:
                connection.execute(
                    "UPDATE auth_sessions SET created_at=now()-interval '1 day' WHERE id=%s",
                    (actor["session_id"],),
                )
            assert (await client.get("/api/admin/users")).status_code == 200
            stale = await client.patch(
                "/api/admin/users", json={"action": "revoke-sessions", "userId": child}
            )
            assert stale.status_code == 403
            assert stale.json()["error"] == "FRESH_SESSION_REQUIRED"

    try:
        asyncio.run(run())
    finally:
        if child:
            with psycopg.connect(database_url) as connection:
                connection.execute("DELETE FROM auth_users WHERE id=%s", (child,))
