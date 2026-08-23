import asyncio
import json
import socket
from uuid import uuid4

import httpx
import psycopg
import pytest

from alphadeck_platform.database import Database
from alphadeck_platform.generated_sandbox import wrap_command
from alphadeck_platform.preview import PreviewManager, persisted_target, read_record
from alphadeck_platform.projects import Projects


@pytest.fixture
def preview_context(database_url, actor, tmp_path, monkeypatch):
    project = "preview-" + uuid4().hex
    root = tmp_path / project
    root.mkdir()
    # The fixture uses only Node's built-in HTTP server; no downloaded frontend dependencies.
    (root / "node_modules").mkdir()
    for relative in (
        ".data-agent/finance-run-plan.json",
        "data_file/final/dashboard-data.json",
        "evidence/sources.json",
        "evidence/data_quality.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    outside = tmp_path / "private-host-data.txt"
    outside.write_text("private host data")
    script = (
        "const http=require('http');const fs=require('fs');const payload=JSON.stringify({ready:true,credentialVisible:!!process.env.DEEPSEEK_API_KEY,hostFileVisible:fs.existsSync("
        + json.dumps(str(outside))
        + ")});http.createServer((req,res)=>{res.setHeader('Content-Type','application/json');res.end(payload)}).listen(Number(process.env.PORT),'127.0.0.1');"
    )
    (root / "preview-fixture.cjs").write_text(script)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-private-key")
    monkeypatch.delenv("ALPHADECK_GENERATED_SANDBOX", raising=False)
    monkeypatch.delenv("ALPHADECK_ALLOW_UNSANDBOXED_GENERATED_CODE", raising=False)
    # Use an ephemeral contiguous range so the test cannot interfere with the user's previews.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        start = listener.getsockname()[1]
    monkeypatch.setenv("PREVIEW_PORT_START", str(start))
    monkeypatch.setenv("PREVIEW_PORT_END", str(min(65535, start + 10)))
    with psycopg.connect(database_url) as tx:
        tx.execute(
            "INSERT INTO projects (id,name,owner_id,repo_path,updated_at) VALUES (%s,'Preview contract',%s,%s,now())",
            (project, actor["user_id"], str(root)),
        )
    database = Database(database_url)
    projects = Projects(database, tmp_path)
    yield project, root, database, projects
    with psycopg.connect(database_url) as tx:
        tx.execute("DELETE FROM projects WHERE id=%s", (project,))


def test_real_namespaced_preview_start_cross_process_status_and_explicit_stop(
    preview_context, actor
):
    from alphadeck_platform.auth import Authorization
    from app import create_app

    project, root, database, projects = preview_context

    async def run():
        manager = PreviewManager(
            projects,
            readiness_seconds=12,
            command_factory=lambda directory, _port: wrap_command(
                directory, "node", ["preview-fixture.cjs"]
            ),
        )
        second = PreviewManager(projects)
        try:
            previews = await asyncio.gather(manager.start(project), manager.start(project))
            assert previews[0]["status"] == "running" and previews[0]["pid"] == previews[1]["pid"]
            preview = previews[0]
            async with httpx.AsyncClient(trust_env=False) as client:
                payload = (await client.get(preview["url"])).json()
            assert payload == {"ready": True, "credentialVisible": False, "hostFileVisible": False}
            row = await projects.get(project)
            assert (await second.reconciled(project, row["preview_url"], row["preview_port"]))[
                "status"
            ] == "running"
            adopted = await second.start(project)
            assert adopted["status"] == "running" and adopted["port"] == preview["port"]
            app = create_app(
                database,
                Authorization(database, enabled=True, secret="test-secret"),
                projects_dir=projects.root,
                preview_manager=second,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                cookies={"alphadeck.session_token": actor["cookie"]},
            ) as client:
                assert (await client.get(f"/api/projects/{project}/preview/status")).json()["data"][
                    "status"
                ] == "running"
                ignored = await client.post(f"/api/projects/{project}/preview/stop")
                assert ignored.status_code == 200 and ignored.json()["ignored"]
                assert await manager.probe(preview["url"])
                state = {
                    "schemaVersion": 1,
                    "projectId": project,
                    "requestId": "fixture-request",
                    "status": "completed",
                    "steps": [],
                }
                (root / ".data-agent/generation-state.json").write_text(json.dumps(state))
                report = {
                    "runId": "fixture-request",
                    "passed": True,
                    "status": "passed",
                    "checks": [],
                }
                (root / ".data-agent/validation.json").write_text(json.dumps(report))
                snapshot = await client.get(f"/api/projects/{project}/generation/status")
                assert snapshot.status_code == 200, snapshot.text
                assert snapshot.json()["data"]["status"] == "preview_pending"
                assert snapshot.json()["data"]["missionAcceptanceRequired"] is True
                assert snapshot.json()["data"]["previewUrl"] is None
                stopped = await client.post(
                    f"/api/projects/{project}/preview/stop", json={"intent": "explicit-user-stop"}
                )
                assert stopped.status_code == 200 and stopped.json()["data"]["status"] == "stopped"
            for _ in range(20):
                if manager.processes[project].closing:
                    break
                await asyncio.sleep(0.1)
            assert manager.processes[project].closing
            await asyncio.wait_for(manager.processes[project].child.wait(), 5)
            assert not await manager.probe(preview["url"])
            assert read_record(root)["status"] == "stopped"
            assert (await projects.get(project))["preview_url"] is None
        finally:
            await manager.cleanup_all()
            await second.cleanup_all()

    asyncio.run(run())


def test_preview_probe_rejects_remote_or_ambiguous_persisted_addresses(monkeypatch):
    monkeypatch.setenv("PREVIEW_PORT_START", "4100")
    monkeypatch.setenv("PREVIEW_PORT_END", "4999")
    assert persisted_target("http://localhost:4100", 4100)
    for url, port in (
        ("http://example.com:4100", 4100),
        ("http://user:pass@localhost:4100", 4100),
        ("http://localhost:4100/private", 4100),
        ("http://localhost:4100", 4101),
        ("http://localhost:3000", 3000),
        ("http://localhost:4100/#secret", 4100),
    ):
        assert persisted_target(url, port) is None
