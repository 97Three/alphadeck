import asyncio
import json
from uuid import uuid4

import httpx
import psycopg
import pytest

from alphadeck_platform.auth import Authorization
from alphadeck_platform.database import Database
from alphadeck_platform.projects import Projects
from alphadeck_platform.resource_lock import resource_lock
from app import create_app


@pytest.fixture
def project(database_url, actor, tmp_path):
    project_id = "python-project-" + str(uuid4())
    root = tmp_path / "projects"
    workspace = root / project_id
    workspace.mkdir(parents=True)
    (workspace / "app").mkdir()
    (workspace / "app/page.tsx").write_text("original\n", encoding="utf-8")
    (workspace / ".env.local").write_text("PRIVATE=must-not-appear")
    (workspace / ".env.example").write_text("PRIVATE=")
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO projects (id,name,owner_id,repo_path,settings,updated_at)
               VALUES (%s,'Migration Project',%s,%s,%s,now())""",
            (
                project_id,
                actor["user_id"],
                str(workspace),
                json.dumps({"dataAgent": {"capabilityId": "technical_analysis"}, "custom": True}),
            ),
        )
    database = Database(database_url)
    app = create_app(
        database,
        Authorization(database, enabled=True, secret="test-secret"),
        projects_dir=root,
        file_lock_wait_seconds=0.1,
    )
    yield {
        "id": project_id,
        "root": root,
        "workspace": workspace,
        "app": app,
        "database": database,
        "cookie": actor["cookie"],
    }
    with psycopg.connect(database_url) as connection:
        connection.execute("DELETE FROM projects WHERE id=%s", (project_id,))


def client_for(project):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=project["app"]),
        base_url="http://test",
        cookies={"alphadeck.session_token": project["cookie"]},
    )


def test_project_list_update_and_frontend_shape(project, database_url, actor):
    async def run():
        async with client_for(project) as client:
            projects = (await client.get("/api/projects")).json()["data"]
            assert [row["id"] for row in projects] == [project["id"]]
            assert "repoPath" not in projects[0]
            assert projects[0]["capabilityId"] == "technical_analysis"
            assert projects[0]["preferredCli"] == "pi"
            path = f"/api/projects/{project['id']}"
            bad = await client.put(path, json={"selected_model": "deepseek direct"})
            assert bad.status_code == 400 and bad.json()["error"] == "INVALID_PROJECT_REQUEST"
            response = await client.put(
                path, json={"name": "  New name  ", "selectedModel": "deepseek direct"}
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["name"] == "New name"
            assert response.json()["data"]["selectedModel"] == "deepseek-v4-flash"
            assert (await client.get(path)).json() == response.json()
            with psycopg.connect(database_url) as connection:
                settings = json.loads(
                    connection.execute(
                        "SELECT settings FROM projects WHERE id=%s", (project["id"],)
                    ).fetchone()[0]
                )
                assert settings["custom"] is True
                assert settings["llm"]["credentialEnv"] == "DEEPSEEK_API_KEY"
            with psycopg.connect(database_url) as connection:
                connection.execute(
                    "UPDATE projects SET owner_id=NULL WHERE id=%s", (project["id"],)
                )
            assert (await client.get("/api/projects")).json()["data"] == []
            assert (await client.get(path)).status_code == 404

    asyncio.run(run())


def test_file_browser_filters_secrets_and_symlink_targets(project, tmp_path):
    workspace = project["workspace"]
    (workspace / "node_modules").mkdir()
    (workspace / ".ssh").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (workspace / "escape.txt").symlink_to(outside)
    (workspace / "alias.txt").symlink_to(workspace / ".env.local")

    async def run():
        async with client_for(project) as client:
            path = f"/api/repo/{project['id']}"
            entries = (await client.get(path + "/tree")).json()
            assert {entry["path"] for entry in entries} == {"app", ".env.example"}
            assert entries[0]["type"] == "dir" and entries[0]["hasChildren"]
            file = await client.get(path + "/file", params={"path": "app/page.tsx"})
            assert file.json() == {"path": "app/page.tsx", "content": "original\n"}
            for secret in (".env.local", "alias.txt", ".ssh/id_rsa"):
                response = await client.get(path + "/file", params={"path": secret})
                assert response.status_code == 404
                assert "PRIVATE" not in response.text
            assert (
                await client.get(path + "/file", params={"path": "escape.txt"})
            ).status_code == 400
            nested = await client.get(
                f"/api/projects/{project['id']}/files/content", params={"path": "app/page.tsx"}
            )
            assert nested.json() == {"success": True, "data": file.json()}

    asyncio.run(run())


def test_file_writes_respect_shared_lock_and_preserve_original_file(project):
    target = project["workspace"] / "app/page.tsx"
    target.chmod(0o755)

    async def run():
        async with client_for(project) as client:
            path = f"/api/repo/{project['id']}/file"
            async with resource_lock(project["workspace"]):
                response = await client.put(
                    path, json={"path": "app/page.tsx", "content": "changed"}
                )
                assert response.status_code == 409
                assert target.read_text() == "original\n"
            response = await client.put(path, json={"path": "app/page.tsx", "content": "你好\n"})
            assert response.json() == {"success": True, "path": "app/page.tsx"}
            assert target.read_text() == "你好\n"
            assert target.stat().st_mode & 0o777 == 0o755
            assert not (project["workspace"] / ".pi-workspace.lock").exists()
            assert (
                await client.put(path, json={"path": "missing", "content": "new"})
            ).status_code == 404
            assert (
                await client.put(path, json={"path": ".env.local", "content": "overwrite"})
            ).status_code == 404

    asyncio.run(run())


def test_viewer_can_read_but_cannot_update_delete_or_write(project, database_url, actor):
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE projects SET owner_id=NULL WHERE id=%s", (project["id"],))
        connection.execute(
            "INSERT INTO project_memberships (id,project_id,user_id,role,updated_at) VALUES (%s,%s,%s,'viewer',now())",
            (str(uuid4()), project["id"], actor["user_id"]),
        )

    async def run():
        async with client_for(project) as client:
            path = f"/api/projects/{project['id']}"
            assert (await client.get(path)).status_code == 200
            assert (await client.get(path + "/files")).status_code == 200
            assert (await client.put(path, json={"name": "wrong"})).status_code == 403
            assert (await client.delete(path)).status_code == 403
            assert (
                await client.put(
                    path + "/files/content", json={"path": "app/page.tsx", "content": "wrong"}
                )
            ).status_code == 403

    asyncio.run(run())


def seed_bucket(database_url, actor):
    bucket_id = str(uuid4())
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO usage_buckets (id,actor_user_id,metric,window_start,window_end,used,updated_at)
               VALUES (%s,%s,'projects.owned','1970-01-01','9999-12-31T23:59:59.999',1,now())""",
            (bucket_id, actor["user_id"]),
        )
    return bucket_id


def test_concurrent_project_delete_decrements_allocation_once(project, database_url, actor):
    bucket_id = seed_bucket(database_url, actor)

    async def run():
        async with client_for(project) as client:
            responses = await asyncio.gather(
                *[client.delete(f"/api/projects/{project['id']}") for _ in range(2)]
            )
            assert sorted(response.status_code for response in responses) == [200, 404]

    asyncio.run(run())
    assert not project["workspace"].exists()
    with psycopg.connect(database_url) as connection:
        assert (
            connection.execute(
                "SELECT used FROM usage_buckets WHERE id=%s", (bucket_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM usage_events WHERE source_id=%s AND source_type='project_deleted'",
                (project["id"],),
            ).fetchone()[0]
            == 1
        )


def test_out_of_root_workspace_rejected_before_deleting_database(
    project, database_url, actor, tmp_path
):
    outside = tmp_path / "other-workspace"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE projects SET repo_path=%s WHERE id=%s", (str(outside), project["id"])
        )

    async def run():
        async with client_for(project) as client:
            assert (await client.delete(f"/api/projects/{project['id']}")).status_code == 500

    asyncio.run(run())
    assert (outside / "keep.txt").read_text() == "keep"
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT id FROM projects WHERE id=%s", (project["id"],)
        ).fetchone()


def test_quota_failure_rolls_back_project_delete_and_preserves_files(project, database_url, actor):
    from psycopg import sql

    bucket_id = seed_bucket(database_url, actor)
    name = "test_delete_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        connection.execute(
            sql.SQL(
                "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.source_id = {} THEN RAISE EXCEPTION 'intentional test failure'; END IF; RETURN NEW; END; $$"
            ).format(sql.Identifier(name), sql.Literal(project["id"]))
        )
        connection.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT ON usage_events FOR EACH ROW EXECUTE FUNCTION {}()"
            ).format(sql.Identifier(name), sql.Identifier(name))
        )
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            asyncio.run(
                Projects(project["database"], project["root"]).delete(
                    project["id"], actor["user_id"]
                )
            )
        assert (project["workspace"] / "app/page.tsx").read_text() == "original\n"
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT id FROM projects WHERE id=%s", (project["id"],)
            ).fetchone()
            assert (
                connection.execute(
                    "SELECT used FROM usage_buckets WHERE id=%s", (bucket_id,)
                ).fetchone()[0]
                == 1
            )
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute(
                sql.SQL("DROP TRIGGER {} ON usage_events").format(sql.Identifier(name))
            )
            connection.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(name)))
