import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql

from alphadeck_platform.auth import Authorization
from alphadeck_platform.config import ROOT
from alphadeck_platform.database import Database
from alphadeck_platform.projects import Projects
from alphadeck_platform.provision import Provisioner
from alphadeck_platform.skill_catalog import SkillCatalogue
from alphadeck_platform.skill_compiler import compile_skills
from app import create_app


@pytest.fixture(scope="module")
def repository(tmp_path_factory):
    root = tmp_path_factory.mktemp("project-source")
    shutil.copytree(ROOT / ".pi", root / ".pi")
    shutil.copytree(ROOT / "config", root / "config")
    return root


def configured(database_url, actor, tmp_path, repository, provisioner=None):
    database = Database(database_url)
    provisioner = provisioner or Provisioner(repository)
    app = create_app(
        database,
        Authorization(database, enabled=True, secret="test-secret"),
        projects_dir=tmp_path / "projects",
        project_provisioner=provisioner,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={"alphadeck.session_token": actor["cookie"]},
    )
    return client, provisioner


def test_project_creation_publishes_complete_pinned_workspace(
    database_url, actor, tmp_path, repository
):
    identity = "created-" + str(uuid4())
    client, provisioner = configured(database_url, actor, tmp_path, repository)
    target = tmp_path / "projects" / identity
    observed = []
    original = provisioner.provision

    async def checked(stage, target, fields, application, attempt):
        with psycopg.connect(database_url) as connection:
            status = connection.execute(
                "SELECT status FROM projects WHERE id=%s", (identity,)
            ).fetchone()[0]
        observed.append(status)
        assert not target.exists()
        return await original(stage, target, fields, application, attempt)

    provisioner.provision = checked

    async def run():
        async with client:
            key = str(uuid4())
            body = {
                "projectId": identity,
                "name": "  Python 看板  ",
                "initialPrompt": "研究茅台",
                "capabilityId": "technical_analysis",
                "selectedModel": "deepseek direct",
            }
            response = await client.post(
                "/api/projects", json=body, headers={"idempotency-key": key}
            )
            assert response.status_code == 201, response.text
            project = response.json()["data"]
            assert project["status"] == "idle" and project["name"] == "Python 看板"
            assert project["selectedModel"] == "deepseek-v4-flash"
            workspace = json.loads((target / ".data-agent/workspace.json").read_text())
            assert workspace["composition"]["sha256"] == project["dataAgentCompositionSha256"]
            assert workspace["runtime"]["modelId"] == "deepseek-v4-flash"
            plan = json.loads((target / ".data-agent/finance-run-plan.json").read_text())
            assert plan["status"] == "pending" and plan["capabilityId"] == "technical_analysis"
            assert (target / ".pi/installed-skills.json").is_file()
            pinned = compile_skills(
                repository, phase="planning", runtime_workspace=target, budget=32000
            )
            assert "query-rewrite" in pinned["selectedSkillIds"]
            assert (
                await client.post("/api/projects", json=body, headers={"idempotency-key": key})
            ).status_code == 409
            assert (await client.post("/api/projects", json=body)).status_code == 409
            with psycopg.connect(database_url) as connection:
                assert connection.execute(
                    "SELECT used,reserved FROM usage_buckets WHERE actor_user_id=%s AND metric='projects.owned'",
                    (actor["user_id"],),
                ).fetchone() == (1, 0)
                assert (
                    connection.execute(
                        "SELECT role FROM project_memberships WHERE project_id=%s AND user_id=%s",
                        (identity, actor["user_id"]),
                    ).fetchone()[0]
                    == "owner"
                )
            assert observed == ["initializing"]
            assert (await client.delete("/api/projects/" + identity)).status_code == 200

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM projects WHERE id=%s", (identity,))


def test_creation_quota_rejection_happens_before_workspace_changes(
    database_url, actor, tmp_path, repository
):
    identity = "denied-" + str(uuid4())
    client, _ = configured(database_url, actor, tmp_path, repository)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO user_quota_overrides (id,user_id,metric,"limit",enforcement,window_type,updated_at)
            VALUES (%s,%s,'projects.owned',1,'hard','lifetime',now())""",
            (str(uuid4()), actor["user_id"]),
        )
        connection.execute(
            """INSERT INTO usage_buckets (id,actor_user_id,metric,window_start,window_end,used,updated_at)
            VALUES (%s,%s,'projects.owned','1970-01-01','9999-12-31T23:59:59.999',1,now())""",
            (str(uuid4()), actor["user_id"]),
        )

    async def run():
        async with client:
            response = await client.post(
                "/api/projects", json={"projectId": identity, "name": "Denied"}
            )
            assert response.status_code == 429, response.text
            assert response.json()["quota"]["limit"] == "1"
            assert response.headers["retry-after"].isdigit()

    asyncio.run(run())
    assert not (tmp_path / "projects" / identity).exists()
    with psycopg.connect(database_url) as connection:
        assert (
            connection.execute("SELECT id FROM projects WHERE id=%s", (identity,)).fetchone()
            is None
        )


def test_publish_database_failure_rolls_back_row_files_catalogue_and_quota(
    database_url, actor, tmp_path, repository
):
    identity = "rollback-" + str(uuid4())
    client, _ = configured(database_url, actor, tmp_path, repository)
    name = "test_publish_" + uuid4().hex
    with psycopg.connect(database_url) as connection:
        connection.execute(
            sql.SQL(
                "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id={} AND NEW.status='idle' THEN RAISE EXCEPTION 'intentional publish failure'; END IF; RETURN NEW; END; $$"
            ).format(sql.Identifier(name), sql.Literal(identity))
        )
        connection.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION {}()"
            ).format(sql.Identifier(name), sql.Identifier(name))
        )

    async def run():
        async with client:
            response = await client.post(
                "/api/projects", json={"projectId": identity, "name": "Rollback"}
            )
            assert response.status_code == 500
        target = tmp_path / "projects" / identity
        assert not target.exists()
        assert not list((tmp_path / "projects").glob(".*.provision-*"))
        state = SkillCatalogue(repository).read()
        assert SkillCatalogue.deployment_key(target, "pi-agent") not in state["deployments"]
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute("SELECT id FROM projects WHERE id=%s", (identity,)).fetchone()
                is None
            )
            assert connection.execute(
                "SELECT used,reserved FROM usage_buckets WHERE actor_user_id=%s AND metric='projects.owned'",
                (actor["user_id"],),
            ).fetchone() == (0, 0)

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute(sql.SQL("DROP TRIGGER {} ON projects").format(sql.Identifier(name)))
            connection.execute(sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(name)))


def test_lost_commit_acknowledgement_preserves_committed_project(
    database_url, actor, tmp_path, repository
):
    class LostAcknowledgement(Database):
        calls = 0

        @asynccontextmanager
        async def transaction(self):
            self.calls += 1
            call = self.calls
            async with super().transaction() as connection:
                yield connection
            if call == 2:
                raise psycopg.OperationalError("simulated lost commit acknowledgement")

    identity = "commit-" + str(uuid4())
    root = tmp_path / "projects"
    store = Projects(LostAcknowledgement(database_url), root, Provisioner(repository))
    try:
        project = asyncio.run(
            store.create({"projectId": identity, "name": "Committed"}, actor["user_id"])
        )
        assert project["status"] == "idle"
        assert (root / identity / ".data-agent/workspace.json").exists()
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM projects WHERE id=%s", (identity,))
