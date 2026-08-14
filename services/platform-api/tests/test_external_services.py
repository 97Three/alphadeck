import asyncio
import json

import httpx
import pytest
from test_projects import client_for, project  # noqa: F401

from alphadeck_platform.auth import Authorization
from alphadeck_platform.external_services import ExternalServices, validate_repository_name
from alphadeck_platform.git_repository import GitRepository
from alphadeck_platform.project_services import ProjectServices
from alphadeck_platform.service_tokens import ServiceTokens
from app import create_app


class FakeGit:
    def __init__(self):
        self.operations = []

    async def connect(self, root, user, url):
        self.operations.append(("connect", root, user["login"], url))

    async def push(self, root, user, data, token):
        assert token == "synthetic-github-only"
        self.operations.append(("push", root, user["login"], data["clone_url"]))
        return True


def configure(project, handler, git=None):  # noqa: F811
    project["app"] = create_app(
        project["database"],
        Authorization(project["database"], enabled=True, secret="test-secret"),
        projects_dir=project["root"],
        external_service_factory=lambda projects: ExternalServices(
            projects, transport=httpx.MockTransport(handler), git=git
        ),
    )


async def admin(project, actor):  # noqa: F811
    async with project["database"].transaction() as tx:
        await tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))
    for provider in ("github", "supabase", "vercel"):
        await ServiceTokens(project["database"]).create(provider, "synthetic-" + provider + "-only")


def test_github_native_routes_records_and_project_scoping(project, actor):  # noqa: F811
    calls, git = [], FakeGit()

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-github-only"
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "fixture", "name": None, "email": None})
        if request.method == "POST":
            body = json.loads(request.content)
            assert body == {
                "name": "test-repo",
                "description": "",
                "private": True,
                "auto_init": False,
            }
            return httpx.Response(
                201,
                json={
                    "html_url": "https://github.com/fixture/test-repo",
                    "clone_url": "https://github.com/fixture/test-repo.git",
                    "default_branch": "main",
                },
            )
        return httpx.Response(200 if request.url.path.endswith("exists") else 404, json={})

    configure(project, handler, git)

    async def run():
        await admin(project, actor)
        async with client_for(project) as client:
            assert (await client.get("/api/github/check-repo/new-repo")).json() == {
                "available": True,
                "username": "fixture",
            }
            assert (await client.get("/api/github/check-repo/exists")).status_code == 409
            assert (await client.get("/api/github/check-repo/con")).status_code == 400
            created = await client.post(
                "/api/github/create-repo", json={"repo_name": "test-repo", "private": True}
            )
            assert created.json()["owner"] == "fixture"
            base = "/api/projects/" + project["id"]
            connected = await client.post(
                base + "/github/connect", json={"repo_name": "test-repo", "private": True}
            )
            assert connected.status_code == 200, connected.text
            assert git.operations[0][1] == project["workspace"]
            assert (await client.post(base + "/github/push")).status_code == 200
            records = (await client.get(base + "/services")).json()
            assert len(records) == 1
            assert records[0]["service_data"] == records[0]["serviceData"]
            assert records[0]["serviceData"]["last_pushed_at"].endswith("Z")
            assert "synthetic" not in json.dumps(records)
            service_id = records[0]["id"]
            other = await client.delete("/api/projects/missing/services/" + service_id)
            assert other.status_code == 404
            assert (await client.delete(base + "/services/" + service_id)).status_code == 200
            assert (await client.post(base + "/github/push")).status_code == 404
        assert len(git.operations) == 2

    asyncio.run(run())


def test_supabase_native_management_and_secret_keys(project, actor):  # noqa: F811
    def handler(request):
        assert request.url.host == "api.supabase.com"
        if request.url.path.endswith("/organizations"):
            value = [{"id": "org", "slug": "org", "name": "Test org"}]
        elif request.url.path.endswith("/api-keys"):
            value = [
                {"id": "anon", "api_key": "test-public"},
                {"name": "service_role", "api_key": "test-private"},
            ]
        else:
            value = {
                "id": "sb-test",
                "name": "SB",
                "status": "ACTIVE_HEALTHY",
                "region": "us-east-1",
                "organization_id": "org",
            }
        if request.method == "POST":
            assert json.loads(request.content) == {
                "organization_id": "org",
                "name": "SB",
                "region": "us-east-1",
                "db_pass": "synthetic-password",
            }
        return httpx.Response(200, json=value)

    configure(project, handler)

    async def run():
        await admin(project, actor)
        async with client_for(project) as client:
            assert (
                len((await client.get("/api/supabase/organizations")).json()["organizations"]) == 1
            )
            created = await client.post(
                "/api/supabase/create-project",
                json={
                    "projectId": project["id"],
                    "project_name": "SB",
                    "organizationId": "org",
                    "db_pass": "synthetic-password",
                },
            )
            assert created.status_code == 200, created.text
            assert created.json()["project_id"] == "sb-test"
            assert (await client.get("/api/supabase/projects/sb-test")).json()["project"][
                "name"
            ] == "SB"
            keys = await client.get("/api/supabase/projects/sb-test/api-keys")
            assert keys.json()["keys"] == {"anon": "test-public", "service_role": "test-private"}
            assert keys.headers["cache-control"] == "private, no-store"
            connected = await client.post(
                f"/api/projects/{project['id']}/supabase/connect",
                json={"supabase_project_id": "other", "project_url": "https://other.supabase.co"},
            )
            assert connected.json()["data"]["project_id"] == "other"
            records = await ProjectServices(project["database"]).list(project["id"])
            assert len(records) == 1 and "synthetic-password" not in json.dumps(records)

    asyncio.run(run())


def test_vercel_native_connect_conflict_env_deploy_and_status_fallback(project, actor):  # noqa: F811
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.host == "api.github.com":
            return httpx.Response(
                200, json={"id": 77, "name": "test-repo", "default_branch": "main"}
            )
        assert request.url.params["teamId"] == "team"
        if request.url.path == "/v9/projects":
            return httpx.Response(200, json={"projects": [{"name": "existing"}]})
        if request.url.path == "/v10/projects":
            return httpx.Response(409, json={})
        if request.url.path == "/v9/projects/site":
            return httpx.Response(200, json={"id": "prj_test", "name": "site"})
        if request.url.path.endswith("/env"):
            assert json.loads(request.content)["value"] == "test-environment-only"
            return httpx.Response(409, json={})
        if request.method == "POST":
            assert json.loads(request.content)["gitSource"] == {
                "type": "github",
                "repoId": 77,
                "ref": "feature",
            }
            return httpx.Response(
                200, json={"id": "deployment", "url": "site.example.test", "readyState": "QUEUED"}
            )
        if request.url.path == "/v13/deployments/deployment":
            return httpx.Response(404, json={})
        return httpx.Response(
            200,
            json={
                "deployments": [
                    {
                        "id": "replacement",
                        "url": "site.example.test",
                        "readyState": "READY",
                        "createdAt": 1700000000000,
                    }
                ]
            },
        )

    configure(project, handler)

    async def run():
        await admin(project, actor)
        await ProjectServices(project["database"]).save(
            project["id"],
            "github",
            {"owner": "fixture", "repo_name": "test-repo", "default_branch": "feature"},
        )
        async with client_for(project) as client:
            await client.post(
                f"/api/env/{project['id']}", json={"key": "TOKEN", "value": "test-environment-only"}
            )
            assert (await client.get("/api/vercel/check-project/existing?teamId=team")).json() == {
                "available": False
            }
            base = f"/api/projects/{project['id']}/vercel"
            connected = await client.post(
                base + "/connect", json={"project_name": "site", "teamId": "team"}
            )
            assert connected.status_code == 200, connected.text
            assert connected.json()["data"]["github_repo"] == "fixture/test-repo"
            deployed = await client.post(base + "/deploy")
            assert deployed.json()["deployment_url"] == "https://site.example.test"
            status = (await client.get(base + "/deployment/current")).json()
            assert status["status"] == "READY" and not status["has_deployment"]
            assert status["deployment_id"] == "replacement"
        assert ("GET", "/v13/projects/prj_test/deployments") in calls

    asyncio.run(run())


def test_external_errors_are_safe_and_permissions_precede_upstream_calls(project, actor):  # noqa: F811
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, text="synthetic-private-upstream-value")

    configure(project, handler)

    async def run():
        await ServiceTokens(project["database"]).create("github", "synthetic-github-only")
        async with client_for(project) as client:
            assert (await client.get("/api/github/check-repo/valid")).status_code == 403
            assert calls == []
            await admin(project, actor)
            failed = await client.get("/api/github/check-repo/valid")
            assert failed.status_code == 403
            assert "synthetic-private" not in failed.text
            assert len(calls) == 1
        # Read-only unconfigured deployment state is an ordinary UI state.
        service = ProjectServices(project["database"])
        assert await service.get(project["id"], "vercel") is None

    asyncio.run(run())


def test_native_git_initialization_does_not_commit_environment_or_dependencies(tmp_path):
    (tmp_path / ".env.local").write_text("PRIVATE=test-value")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/dependency.js").write_text("ignored")
    (tmp_path / "page.tsx").write_text("export default null")

    async def run():
        git = GitRepository()
        await git.connect(tmp_path, {"login": "fixture"}, "https://github.com/fixture/test.git")
        tracked = (await git.run(tmp_path, "ls-files")).splitlines()
        assert tracked == [".gitignore", "page.tsx"]
        assert not await git.commit(tmp_path, "No changes")
        assert (
            await git.run(tmp_path, "remote", "get-url", "origin")
            == "https://github.com/fixture/test.git"
        )

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,valid",
    [
        ("abc", True),
        ("A_b.c-d", True),
        ("con", False),
        ("x..y", False),
        ("../bad", False),
        (".x", False),
        ("x" * 101, False),
    ],
)
def test_repository_name_validation(name, valid):
    assert bool(validate_repository_name(name)) is not valid
