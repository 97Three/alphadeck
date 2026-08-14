import asyncio
import json
from uuid import uuid4

from test_projects import client_for, project  # noqa: F401

from alphadeck_platform.auth import Authorization
from alphadeck_platform.config import ROOT
from alphadeck_platform.evaluation_platform import EvaluationPlatform
from alphadeck_platform.workspace import write_atomic
from app import create_app


def test_evaluation_dashboard_queue_cases_sets_reports_and_repairs(
    project,  # noqa: F811
    actor,
    tmp_path,
    monkeypatch,  # noqa: F811
):
    root = tmp_path / "evaluation"
    root.mkdir()
    for relative in (
        "benchmarks/alphadeck/cases.json",
        "benchmarks/alphadeck/datasets.json",
        "benchmarks/alphadeck/snapshot-manifest.json",
        "benchmarks/alphadeck/judge-calibration.contract.json",
    ):
        write_atomic(root, relative, (ROOT / relative).read_bytes())
    monkeypatch.setenv("ALPHADECK_ADMIN_TOKEN", "controlled-eval-admin")

    async def run():
        database = project["database"]
        async with database.transaction() as tx:
            await tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))
        project["app"] = create_app(
            database,
            Authorization(database, enabled=True, secret="test-secret"),
            projects_dir=project["root"],
            evaluation_root=root,
        )
        service = EvaluationPlatform(database, root)
        identity = "report-" + str(uuid4().int)
        queue_id = None
        try:
            async with client_for(project) as client:
                dashboard = await client.get("/api/evals")
                assert dashboard.status_code == 200, dashboard.text
                data = dashboard.json()["data"]
                assert data["summary"]["caseCount"] == 16
                assert data["assurance"]["judge"] is not None
                denied = await client.post("/api/evals", json={"action": "start-benchmark"})
                assert denied.status_code == 401
                headers = {"X-AlphaDeck-Admin-Token": "controlled-eval-admin"}
                created = await client.post(
                    "/api/evals",
                    headers=headers,
                    json={
                        "action": "create-case",
                        "id": "fixture-test",
                        "name": "Controlled test",
                        "question": "分析 600519",
                        "capabilityId": "stock_diagnosis",
                    },
                )
                assert created.status_code == 200, created.text
                assert created.json()["data"]["id"] == "fixture-test"
                selected = await client.post(
                    "/api/evals",
                    headers=headers,
                    json={
                        "action": "create-eval-set",
                        "name": "Controlled set",
                        "caseIds": ["fixture-test", "absent"],
                    },
                )
                assert selected.status_code == 200 and selected.json()["data"]["caseIds"] == [
                    "fixture-test"
                ]
                queued = await client.post(
                    "/api/evals",
                    headers=headers,
                    json={
                        "action": "start-benchmark",
                        "selectedCases": ["fixture-test"],
                        "repeat": 2,
                        "concurrency": 3,
                    },
                )
                assert queued.status_code == 200, queued.text
                item = queued.json()["data"]
                queue_id = item["id"]
                assert (
                    item["status"] == "queued" and item["repeat"] == 2 and item["concurrency"] == 3
                )
                cancelled = await client.post(
                    "/api/evals",
                    headers=headers,
                    json={"action": "cancel-benchmark", "queueId": queue_id},
                )
                assert cancelled.json()["data"]["status"] == "cancelled"
                raw = {
                    "createdAt": "2026-11-03T00:00:00.000Z",
                    "passed": False,
                    "results": [
                        {
                            "id": "fixture-test",
                            "passed": False,
                            "failures": ["controlled build failure"],
                            "validation": {
                                "status": "failed",
                                "checks": [
                                    {
                                        "id": "vue_build",
                                        "status": "failed",
                                        "summary": "controlled",
                                    }
                                ],
                            },
                        }
                    ],
                }
                path = "tmp/alphadeck-benchmark-reports/" + identity + ".json"
                write_atomic(root, path, json.dumps(raw).encode())
                report = await client.get("/api/evals/runs/" + identity)
                assert report.status_code == 200 and report.json()["data"]["failedCount"] == 1
                await service.create_repairs(report.json()["data"])
                await service.create_repairs(report.json()["data"])
                tickets = [
                    ticket for ticket in await service.tickets() if ticket["runId"] == identity
                ]
                assert len(tickets) == 1 and tickets[0]["severity"] == "high"
                assert "Vue/Vite build" in tickets[0]["suggestedActions"][0]
                assert (await client.get("/api/evals/runs/invalid")).status_code == 404
                simulation = await client.post(
                    "/api/evals",
                    headers=headers,
                    json={"action": "simulate-flow", "selectedCases": ["absent"]},
                )
                assert simulation.status_code == 200 and simulation.json()["data"]["ready"] is False
                assert "alphadeck_platform.benchmark_cli" in simulation.json()["data"]["command"]
                (root / path).unlink()
                restored = await client.get("/api/evals/runs/" + identity)
                assert restored.status_code == 200 and restored.json()["data"]["id"] == identity
        finally:
            async with database.transaction() as tx:
                await tx.execute("DELETE FROM eval_repair_tickets WHERE run_id=%s", (identity,))
                await tx.execute("DELETE FROM eval_runs WHERE id=%s", (identity,))
                if queue_id:
                    await tx.execute("DELETE FROM eval_queue_items WHERE id=%s", (queue_id,))

    asyncio.run(run())
