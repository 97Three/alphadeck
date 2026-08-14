import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_projects import client_for, project  # noqa: F401

from alphadeck_platform.secret_crypto import decrypt, encrypt, resolve_key

CASES = json.loads((Path(__file__).parent / "fixtures/crypto.json").read_text())


@pytest.mark.parametrize("case", CASES["cases"])
def test_encrypted_storage_is_compatible_with_original_node_backend(case):
    key = bytes.fromhex(CASES["key"])
    assert decrypt(case["encoded"], key=key) == case["expected"]
    if case["algorithm"].endswith("gcm"):
        assert encrypt(case["text"], key=key, nonce=bytes.fromhex(case["nonce"])) == case["encoded"]
        tampered = case["encoded"][:-1] + ("1" if case["encoded"][-1] == "0" else "0")
        with pytest.raises(ValueError):
            decrypt(tampered, key=key)


def test_key_configuration_and_empty_values():
    with pytest.raises(ValueError, match="stable 64-character"):
        resolve_key({"NODE_ENV": "production"})
    assert resolve_key({}) == resolve_key(
        {"NEXT_PHASE": "phase-production-build", "NODE_ENV": "production"}
    )
    # The original writer allowed empty values; keep them readable rather than losing an env row.
    assert decrypt(encrypt("")) == ""


def test_project_environment_is_encrypted_masked_and_synchronized(project, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ENCRYPTION_KEY", CASES["key"])

    async def run():
        async with client_for(project) as client:
            base = f"/api/env/{project['id']}"
            private_value = "fixture-value-only-123456"
            created = await client.post(
                base,
                json={"key": "SERVICE_FIXTURE", "value": private_value, "description": " test "},
            )
            assert created.status_code == 201, created.text
            data = created.json()["data"]
            assert data["value"] == "" and data["valuePreview"] == "fi••••56"
            row = await project["database"].fetch_one(
                "SELECT value_encrypted FROM env_vars WHERE project_id=%s AND key='SERVICE_FIXTURE'",
                (project["id"],),
            )
            assert (
                row["value_encrypted"].startswith("v2:")
                and private_value not in row["value_encrypted"]
            )
            assert decrypt(row["value_encrypted"]) == private_value
            assert private_value in (project["workspace"] / ".env").read_text()
            duplicate = await client.post(base, json={"key": "SERVICE_FIXTURE", "value": "again"})
            assert duplicate.status_code == 409
            listing = await client.get(base)
            assert listing.headers["cache-control"] == "private, no-store"
            assert private_value not in listing.text
            public = await client.post(
                base + "/upsert",
                json={"key": "PUBLIC_FIXTURE", "value": "hello world", "isSecret": False},
            )
            assert public.json()["data"]["value"] == "hello world"
            assert not (await client.get(base + "/conflicts")).json()["has_conflicts"]
            updated = await client.put(base + "/SERVICE_FIXTURE", json={"value": ""})
            assert updated.status_code == 200
            assert (
                next(
                    item
                    for item in (await client.get(base)).json()
                    if item["key"] == "SERVICE_FIXTURE"
                )["hasValue"]
                is False
            )
            (project["workspace"] / ".env").write_text(
                "PUBLIC_FIXTURE=changed\nFILE_FIXTURE='from-file'\n"
            )
            conflicts = (await client.get(base + "/conflicts")).json()
            assert {item["conflict_type"] for item in conflicts["conflicts"]} == {
                "file_only",
                "db_only",
                "value_mismatch",
            }
            synced = await client.post(base + "/sync/file-to-db")
            assert synced.json()["synced_count"] == 3
            listed = (await client.get(base)).json()
            assert {item["key"] for item in listed} == {"PUBLIC_FIXTURE", "FILE_FIXTURE"}
            assert (await client.post(base + "/sync/db-to-file")).json()["synced_count"] == 2
            assert (await client.delete(base + "/FILE_FIXTURE")).status_code == 200
            invalid = await client.post(base, json={"key": "BAD=KEY", "value": "x"})
            assert invalid.status_code == 400
            target = project["workspace"] / ".env"
            target.unlink()
            target.symlink_to(project["workspace"] / ".env.local")
            with pytest.raises(ValueError):
                await client.post(base + "/sync/db-to-file")
            assert (project["workspace"] / ".env.local").read_text() == "PRIVATE=must-not-appear"

    asyncio.run(run())


def test_service_token_storage_never_returns_plaintext_to_browser(project, actor, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ENCRYPTION_KEY", CASES["key"])
    internal = "synthetic-internal-" + uuid4().hex
    monkeypatch.setenv("ALPHADECK_INTERNAL_API_TOKEN", internal)
    monkeypatch.setenv("ALPHADECK_ENABLE_INTERNAL_TOKEN_API", "1")

    async def run():
        async with project["database"].transaction() as tx:
            await tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))
        async with client_for(project) as client:
            synthetic = "fixture-token-only-" + uuid4().hex
            created = await client.post(
                "/api/tokens",
                json={"provider": "github", "token": synthetic, "name": " Test token "},
            )
            assert created.status_code == 201, created.text
            data = created.json()["data"]
            assert data["token"] is None and data["has_token"] and synthetic not in created.text
            row = await project["database"].fetch_one(
                "SELECT token FROM service_tokens WHERE id=%s", (data["id"],)
            )
            assert row["token"].startswith("v2:") and decrypt(row["token"]) == synthetic
            listing = await client.get("/api/tokens/github")
            assert (
                synthetic not in listing.text
                and listing.headers["cache-control"] == "private, no-store"
            )
            denied = await client.get("/api/tokens/internal/github/token")
            assert denied.status_code == 403
            revealed = await client.get(
                "/api/tokens/internal/github/token",
                headers={"x-alphadeck-internal-token": internal},
            )
            assert revealed.json() == {"token": synthetic}
            monkeypatch.setenv("ALPHADECK_ENABLE_INTERNAL_TOKEN_API", "0")
            assert (
                await client.get(
                    "/api/tokens/internal/github/token",
                    headers={"x-alphadeck-internal-token": internal},
                )
            ).status_code == 404
            assert (await client.delete("/api/tokens/" + data["id"])).status_code == 200
            assert (await client.get("/api/tokens/github")).status_code == 404

    asyncio.run(run())
