import asyncio
from uuid import uuid4

import httpx
from test_projects import client_for, project  # noqa: F401
from test_skill_administration import skills_root  # noqa: F401

from alphadeck_platform.auth import Authorization
from alphadeck_platform.strategy_mappers import CATALOG
from app import create_app


def test_native_strategy_dashboard_capability_center_and_metered_replay(
    project,  # noqa: F811
    actor,
    skills_root,  # noqa: F811
    tmp_path,  # noqa: F811
):  # noqa: F811
    async def run():
        database, calls = project["database"], []
        async with database.transaction() as tx:
            await tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))

        def handler(request):
            calls.append(request)
            if request.url.path == "/api/v1/registry":
                payload = {
                    "providers": [
                        {"id": "fixture", "name": "Controlled fixture", "status": "available"}
                    ]
                }
            elif request.url.path.endswith("/universes/summary"):
                payload = {
                    "universes": [
                        {"id": CATALOG["sampleUniverseId"], "stock_count": 1, "member_count": 1}
                    ]
                }
            elif request.url.path.endswith("/members"):
                payload = {
                    "members": [{"symbol": "600519", "name": "贵州茅台"}],
                    "total": 1,
                    "page": 1,
                    "page_size": 10,
                }
            elif request.url.path.endswith("/status"):
                payload = {"components": [{"id": "data", "name": "Data", "status": "ready"}]}
            else:
                payload = {}
            return httpx.Response(200, json=payload)

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
                center = await client.get("/api/quant/capability-center")
                assert center.status_code == 200, center.text
                assert center.json()["data"]["summary"]["marketApiReachable"]
                assert center.json()["data"]["summary"]["skills"] == 12
                key = "strategy-dashboard-" + uuid4().hex
                first = await client.get("/api/quant/strategies", headers={"idempotency-key": key})
                assert first.status_code == 200, first.text
                data = first.json()["data"]
                assert data["summary"]["templates"] == 7
                assert data["research"]["source"] == "market-api"
                assert data["research"]["universes"][0]["members"][0]["symbol"] == "600519"
                before = len(calls)
                replay = await client.get("/api/quant/strategies", headers={"idempotency-key": key})
                assert (
                    replay.headers["idempotency-replayed"] == "true"
                    and replay.json() == first.json()
                )
                assert len(calls) == before
                operation = await database.fetch_one(
                    "SELECT * FROM api_idempotency_operations WHERE scope='quant-strategies' AND actor_key=%s",
                    (actor["user_id"],),
                )
                assert (
                    operation["status"] == "completed"
                    and operation["quota_accounted_at"] is not None
                )
                prompt = await client.post(
                    "/api/quant/strategies",
                    json={"templateId": "ma-crossover-single-asset", "symbol": "600519"},
                )
                assert prompt.status_code == 201 and "600519" in prompt.json()["data"]["prompt"]
                assert (
                    await client.post("/api/quant/strategies", json={"action": "unrecognized"})
                ).status_code == 400

    asyncio.run(run())


def test_native_parameter_scans_persist_best_result_and_background_job_once(
    project,  # noqa: F811
    actor,
    tmp_path,  # noqa: F811
):  # noqa: F811
    async def run():
        database, calls, run_ids, job_ids = project["database"], [], [], []
        async with database.transaction() as tx:
            await tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))

        def handler(request):
            calls.append(request)
            assert "/backtests/strategies/ma_crossover/600519" in request.url.path
            fast, slow = (
                int(request.url.params["fast_window"]),
                int(request.url.params["slow_window"]),
            )
            return httpx.Response(
                200,
                json={
                    "summary": {
                        "total_return_pct": slow - fast,
                        "max_drawdown_pct": 8,
                        "trade_count": 3,
                    }
                },
            )

        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as market:
                project["app"] = create_app(
                    database,
                    Authorization(database, enabled=True, secret="test-secret"),
                    projects_dir=project["root"],
                    market_client=market,
                    strategy_scans_root=tmp_path / "scans",
                )
                async with client_for(project) as client:
                    key = "strategy-scan-" + uuid4().hex
                    body = {
                        "action": "run-scan-now",
                        "templateId": "ma-crossover-single-asset",
                        "scanId": "ma-window-grid",
                        "symbol": "600519",
                        "idempotencyKey": key,
                    }
                    response = await client.post("/api/quant/strategies", json=body)
                    assert response.status_code == 201, response.text
                    run = response.json()["data"]
                    run_ids.append(run["id"])
                    assert run["status"] == "completed" and run["succeeded"] > 0
                    best = next(
                        item for item in run["results"] if item["id"] == run["bestResultId"]
                    )
                    assert best["metrics"]["totalReturnPct"] == max(
                        item["metrics"]["totalReturnPct"]
                        for item in run["results"]
                        if item["status"] == "success"
                    )
                    before = len(calls)
                    assert (await client.post("/api/quant/strategies", json=body)).headers[
                        "idempotency-replayed"
                    ] == "true"
                    assert len(calls) == before
                    conflict = await client.post(
                        "/api/quant/strategies", json={**body, "symbol": "000001"}
                    )
                    assert conflict.status_code == 409
                    queued = await client.post(
                        "/api/quant/strategies",
                        json={
                            **body,
                            "action": "run-scan",
                            "idempotencyKey": "queued-" + uuid4().hex,
                        },
                    )
                    assert queued.status_code == 201, queued.text
                    job = queued.json()["data"]
                    job_ids.append(job["id"])
                    assert job["status"] == "queued"
                    stored = await database.fetch_one(
                        "SELECT * FROM strategy_scan_jobs WHERE id=%s", (job["id"],)
                    )
                    run_ids.append(stored["run_id"])
                    assert stored["status"] == "completed"
                    assert (tmp_path / "scans/runs" / (run["id"] + ".json")).is_file()
        finally:
            async with database.transaction() as tx:
                if job_ids:
                    await tx.execute("DELETE FROM strategy_scan_jobs WHERE id=ANY(%s)", (job_ids,))
                if run_ids:
                    await tx.execute("DELETE FROM strategy_scan_runs WHERE id=ANY(%s)", (run_ids,))

    asyncio.run(run())
