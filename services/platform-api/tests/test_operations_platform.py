import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_projects import client_for, project  # noqa: F401
from test_skill_administration import skills_root  # noqa: F401

from alphadeck_platform.auth import Authorization
from alphadeck_platform.evaluation_queue import date
from alphadeck_platform.operations_logs import parse_log_entry
from alphadeck_platform.operations_profiles import (
    project_profile,
    runtime_profile,
    strategy_profile,
    summary,
)
from app import create_app

FIXTURES = json.loads((Path(__file__).parent / "fixtures/operations-profiles.json").read_text())


@pytest.mark.parametrize("fixture", FIXTURES)
def test_original_operations_profiles_and_logs(fixture):
    kind = fixture["kind"]
    actual = (
        project_profile(fixture["workspace"])
        if kind == "project"
        else runtime_profile(fixture["systemChecks"], [], fixture["logSources"])
        if kind == "runtime"
        else strategy_profile(fixture["strategy"], now=date("2026-11-03T00:00:00Z"))
        if kind == "strategy"
        else summary(fixture["checks"], fixture["logs"])
        if kind == "summary"
        else parse_log_entry(fixture["raw"], 3, "2026-11-03T00:00:00.000Z")
    )
    assert actual == fixture["expected"]


def test_native_operations_dashboard_uses_persisted_metrics_and_python_runtime(
    project,  # noqa: F811
    actor,
    skills_root,  # noqa: F811
    tmp_path,
    monkeypatch,  # noqa: F811
):
    async def run():
        database = project["database"]
        async with database.transaction() as tx:
            await tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))

        async def compose():
            return {"available": True, "running": False, "service": None}

        monkeypatch.setattr("alphadeck_platform.operations_api.compose_status", compose)

        def handler(request):
            if request.url.path == "/api/v1/registry":
                value = {
                    "providers": [{"id": "fixture", "name": "Controlled", "status": "available"}]
                }
            elif request.url.path.endswith("/universes/summary"):
                value = {"universes": []}
            else:
                value = {}
            return httpx.Response(200, json=value)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as market:
            project["app"] = create_app(
                database,
                Authorization(database, enabled=True, secret="test-secret"),
                projects_dir=project["root"],
                skills_root=skills_root,
                market_client=market,
                strategy_scans_root=tmp_path / "scans",
            )
            async with client_for(project) as client:
                response = await client.get("/api/ops/platform")
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data["agentWorkers"]["available"] and data["productHealth"]["available"]
                assert {profile["id"] for profile in data["healthProfiles"]} == {
                    "project",
                    "runtime",
                    "strategy",
                }
                assert (
                    next(
                        check for check in data["systemChecks"] if check["id"] == "python-runtime"
                    )["status"]
                    == "ok"
                )
                assert all(source["entries"] == [] for source in data["logSources"])
                assert "private" in response.headers["cache-control"]

    asyncio.run(run())
