import asyncio
from uuid import uuid4

from test_projects import client_for, project  # noqa: F401


def test_artifact_download_serves_binary_and_json_without_exposing_secrets(project):  # noqa: F811
    async def run():
        target = project["workspace"] / "evidence/result.json"
        target.parent.mkdir()
        target.write_text('{"passed":true}')
        image = project["workspace"] / "evidence/chart.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        async with client_for(project) as client:
            base = f"/api/projects/{project['id']}/artifact"
            response = await client.get(base, params={"path": "evidence/result.json"})
            assert response.status_code == 200 and response.json() == {"passed": True}
            assert response.headers["cache-control"] == "no-store"
            binary = await client.get(base, params={"path": "evidence/chart.png"})
            assert (
                binary.content == image.read_bytes()
                and binary.headers["content-type"] == "image/png"
            )
            assert (await client.get(base)).status_code == 400
            for relative in (".env.local", "../outside", "evidence/no-file.json"):
                assert (await client.get(base, params={"path": relative})).status_code >= 400
            link = target.parent / "linked.json"
            link.symlink_to(project["workspace"] / ".env.local")
            assert (
                await client.get(base, params={"path": "evidence/linked.json"})
            ).status_code >= 400

    asyncio.run(run())


def test_retry_initialization_reuses_native_ingress_and_preserves_request_binding(project):  # noqa: F811
    async def run():
        async with project["database"].transaction() as tx:
            await tx.execute(
                "UPDATE projects SET initial_prompt='分析600519',selected_model='deepseek-v4-flash',status='failed' WHERE id=%s",
                (project["id"],),
            )
        identity = "retry-" + uuid4().hex
        async with client_for(project) as client:
            response = await client.post(
                f"/api/projects/{project['id']}/retry-initialization", json={"requestId": identity}
            )
            assert response.status_code == 200, response.text
            payload = response.json()["data"]
            assert payload["project"]["status"] == "initializing"
            assert payload["act"]["status"] == "queued"
            job = await project["database"].fetch_one(
                "SELECT execution_envelope FROM agent_generation_jobs WHERE request_id=%s",
                (identity,),
            )
            assert job["execution_envelope"]["payload"]["isInitialPrompt"] is True
            assert job["execution_envelope"]["payload"]["finalInstruction"] == "分析600519"
            repeat = await client.post(
                f"/api/projects/{project['id']}/retry-initialization", json={"requestId": identity}
            )
            assert repeat.status_code == 409
            row = await project["database"].fetch_one(
                "SELECT status FROM projects WHERE id=%s", (project["id"],)
            )
            assert row["status"] == "failed"
            messages = await project["database"].fetch_all(
                "SELECT id FROM messages WHERE project_id=%s AND request_id=%s AND role='user'",
                (project["id"], identity),
            )
            assert len(messages) == 1

    asyncio.run(run())
