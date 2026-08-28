import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_projects import client_for, project  # noqa: F401

from alphadeck_platform.dispatch import Dispatch
from alphadeck_platform.generation_state import GenerationState
from alphadeck_platform.projects import Projects
from alphadeck_platform.workspace_health import (
    artifact_status,
    data_quality_summary,
    derive_lifecycle,
    inspect_workspace,
)

CASES = json.loads((Path(__file__).parent / "fixtures/workspace-health.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_workspace_health_matches_original_contracts(case):
    function = {
        "lifecycle": derive_lifecycle,
        "quality": data_quality_summary,
        "artifact": artifact_status,
    }[case["kind"]]
    assert function(**case["input"]) == case["expected"]


def test_active_lifecycle_does_not_treat_future_artifacts_as_failed(project):  # noqa: F811
    async def run():
        projects = Projects(project["database"], project["root"])
        row = await projects.get(project["id"])
        initial = await inspect_workspace(projects, row)
        assert initial["health"]["status"] == "failed"
        state = GenerationState(project["workspace"])
        request = str(uuid4())
        await state.start(project["id"], request, "test")
        active = await inspect_workspace(projects, row)
        assert active["lifecycle"]["status"] == "running"
        assert active["health"]["status"] == "warning" and active["health"]["blockers"] == 0
        assert not any(item["status"] == "failed" for item in active["artifacts"])
        value = state.read()
        value["status"] = "needs_clarification"
        state._write(value)
        clarification = await inspect_workspace(projects, row)
        assert clarification["deliverySegment"]["label"] == "等待补充"
        assert "补充当前任务" in clarification["nextActions"][0]
        value["status"] = "cancelled"
        state._write(value)
        cancelled = await inspect_workspace(projects, row)
        assert cancelled["lifecycle"]["status"] == "cancelled"

    asyncio.run(run())


def test_workspace_dashboard_uses_database_queue_even_when_projection_file_is_stale(project, actor):  # noqa: F811
    async def run():
        database = project["database"]
        request = str(uuid4())
        async with database.transaction() as tx:
            await tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))
            await tx.execute(
                "INSERT INTO user_requests (id,project_id,actor_user_id,instruction) VALUES (%s,%s,%s,'test')",
                (request, project["id"], actor["user_id"]),
            )
        await Dispatch(database).enqueue(project["id"], request, "test")
        agent = project["workspace"] / ".data-agent"
        agent.mkdir()
        (agent / "generation-queue.json").write_text(
            json.dumps({"items": [{"status": "completed"}]})
        )
        async with client_for(project) as client:
            result = await client.get("/api/workspaces/health")
            assert result.status_code == 200, result.text
            data = result.json()["data"]
            item = next(item for item in data["projects"] if item["id"] == project["id"])
            assert item["generationQueue"]["queued"] == 1
            assert item["lifecycle"]["status"] == "queued"
            assert result.headers["cache-control"] == "private, no-store"
            assert "PRIVATE=must-not-appear" not in result.text

    asyncio.run(run())


def test_health_does_not_follow_artifact_symlinks(project, tmp_path):  # noqa: F811
    outside = tmp_path / "outside.json"
    outside.write_text('{"status":"ok","sources":["private"]}')
    evidence = project["workspace"] / "evidence"
    evidence.mkdir()
    (evidence / "data_quality.json").symlink_to(outside)

    async def run():
        projects = Projects(project["database"], project["root"])
        data = await inspect_workspace(projects, await projects.get(project["id"]))
        assert data["dataQuality"]["status"] == "failed"
        assert not next(item for item in data["artifacts"] if item["id"] == "data_quality")[
            "exists"
        ]
        assert "private" not in json.dumps(data)

    asyncio.run(run())
