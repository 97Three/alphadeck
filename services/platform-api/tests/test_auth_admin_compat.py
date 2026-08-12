import asyncio
from uuid import uuid4

import psycopg
from test_credentials import client_for


def test_enabled_better_auth_admin_client_contract(database_url, actor, tmp_path):
    _, client = client_for(database_url, actor, tmp_path)
    client.cookies.set("alphadeck.session_token", actor["cookie"])
    identities = []

    async def run():
        async with client:
            assert (await client.get("/api/auth/admin/list-users")).status_code == 403
            with psycopg.connect(database_url) as tx:
                tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))
            email = str(uuid4()) + "@example.test"
            created = await client.post(
                "/api/auth/admin/create-user",
                json={
                    "email": email,
                    "name": "Plugin contract",
                    "password": "Plugin-Password-428!",
                },
            )
            assert created.status_code == 200, created.text
            identity = created.json()["user"]["id"]
            identities.append(identity)
            listed = await client.get(
                "/api/auth/admin/list-users",
                params={"searchValue": email, "searchField": "email", "limit": "1", "offset": "0"},
            )
            assert listed.json()["total"] == 1 and listed.json()["users"][0]["id"] == identity
            assert (await client.get("/api/auth/admin/get-user", params={"id": identity})).json()[
                "email"
            ] == email
            updated = await client.post(
                "/api/auth/admin/update-user",
                json={"userId": identity, "data": {"name": "Updated"}},
            )
            assert updated.json()["user"]["name"] == "Updated"
            assert (
                await client.post(
                    "/api/auth/admin/update-user",
                    json={"userId": identity, "data": {"role": "admin"}},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/auth/admin/set-role", json={"userId": identity, "role": "admin"}
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/auth/admin/set-user-password",
                    json={"userId": identity, "newPassword": "Updated-Password-684!"},
                )
            ).json() == {"status": True}
            assert (
                await client.post("/api/auth/admin/list-user-sessions", json={"userId": identity})
            ).json() == {"sessions": []}
            assert (
                await client.post("/api/auth/admin/revoke-user-sessions", json={"userId": identity})
            ).json() == {"success": True}
            for permissions, expected in (
                ({"user": ["create", "list"]}, True),
                ({"user": ["set-role"]}, False),
            ):
                checked = await client.post(
                    "/api/auth/admin/has-permission", json={"permissions": permissions}
                )
                assert checked.json() == {"success": expected}

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as tx:
            tx.execute("DELETE FROM auth_users WHERE id=ANY(%s)", (identities,))
