import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_projects import client_for, project  # noqa: F401

from alphadeck_platform import generation_trace as trace
from alphadeck_platform.generation_state import GenerationState

CASES = json.loads((Path(__file__).parent / "fixtures/generation-trace.json").read_text())


def test_trace_timestamps_preserve_the_instant_across_timezones():
    assert trace.date_iso("2026-11-03T08:30:00+08:00") == "2026-11-03T00:30:00.000Z"


@pytest.mark.parametrize("case", CASES)
def test_generation_trace_projection_matches_original(case):
    assert getattr(trace, case["kind"])(*case["args"]) == case["expected"]


def test_native_trace_aggregates_real_requests_and_recovers_after_success(project, actor):  # noqa: F811
    async def run():
        database = project["database"]
        request, tool = str(uuid4()), str(uuid4())
        async with database.transaction() as tx:
            await tx.execute(
                "INSERT INTO user_requests (id,project_id,actor_user_id,instruction,status) VALUES (%s,%s,%s,'test trace','completed')",
                (request, project["id"], actor["user_id"]),
            )
            await tx.execute(
                "INSERT INTO tool_usages (id,project_id,tool_name,tool_input,tool_output,duration_ms,error) VALUES (%s,%s,'Read','{}',NULL,4,'earlier failure')",
                (tool, project["id"]),
            )
        state = GenerationState(project["workspace"])
        await state.start(project["id"], request, "test")
        agent = project["workspace"] / ".data-agent"
        (agent / "validation.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "checks": [],
                    "updatedAt": "2099-01-01T00:00:00.000Z",
                    "runId": request,
                    "reportPath": ".data-agent/validation.json",
                }
            )
        )
        async with client_for(project) as client:
            assert (await client.get("/api/observability/generation")).status_code == 403
            async with database.transaction() as tx:
                await tx.execute(
                    "UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],)
                )
            response = await client.get("/api/workspaces/trace?events=25")
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            item = next(item for item in data["projects"] if item["id"] == project["id"])
            assert item["trace"]["errorCount"] == 1
            assert item["trace"]["status"] == "success"
            assert item["trace"]["requestCount"] == 1 and item["trace"]["toolCallCount"] == 1
            assert item["topTools"] == [
                {"name": "Read", "count": 1, "errorCount": 1, "averageDurationMs": 4}
            ]
            assert len(item["stages"]) == 8
            summary = (await client.get("/api/workspaces/trace?summary=1")).json()["data"]
            assert summary["projects"] == [] and summary["summary"]["requests"] == 1
            assert (await client.get("/api/observability/generation")).status_code == 200
            assert response.headers["cache-control"] == "private, no-store"

    asyncio.run(run())
