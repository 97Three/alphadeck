import asyncio
import os
import shutil
import sys

import pytest
from test_evaluation_queue import enqueue, queue  # noqa: F401

from alphadeck_platform.config import ROOT
from alphadeck_platform.evaluation_queue import virtual_item
from alphadeck_platform.evaluation_worker import EvaluationWorker

SCRIPT = """import json, os, pathlib, sys, time
root=pathlib.Path(os.environ["ALPHADECK_EVAL_ROOT"])
if sys.argv[1]=="wait":
    time.sleep(60)
else:
    path=root/"tmp/alphadeck-benchmark-reports"/(os.environ["ALPHADECK_EVAL_REPORT_ID"]+".json")
    path.parent.mkdir(parents=True,exist_ok=True)
    body={"passed":sys.argv[1]!="failed","results":[],"metadata":{"queue":{"id":os.environ["ALPHADECK_EVAL_QUEUE_ID"],"leaseToken":os.environ["ALPHADECK_EVAL_LEASE_TOKEN"] if sys.argv[1]!="forged" else "not-owner"},"suite":{"mode":"contract"},"runtime":{"configuredModel":sys.argv[2]}}}
    path.write_text(json.dumps(body))
    sys.exit(1 if sys.argv[1]=="nonzero" else 0)
"""


@pytest.mark.parametrize(
    "scenario,expected",
    [("passed", "passed"), ("failed", "failed"), ("forged", "failed"), ("nonzero", "failed")],
)
def test_native_worker_requires_owned_report_and_successful_process(
    queue,  # noqa: F811
    tmp_path,
    scenario,
    expected,  # noqa: F811
):  # noqa: F811
    script = tmp_path / "controlled_runner.py"
    script.write_text(SCRIPT)

    async def run():
        store, prefix = queue
        await enqueue(store, prefix, 1)
        claim = await store.claim()
        worker = EvaluationWorker(
            store.database,
            root=tmp_path,
            command_factory=lambda _claim, _report: [
                sys.executable,
                "-B",
                str(script),
                scenario,
                claim["item"]["model"],
            ],
            heartbeat_ms=20,
        )
        report_ids = []
        try:
            await worker.execute(claim)
            item = next(item for item in await store.list() if item["id"] == claim["item"]["id"])
            if item["reportId"]:
                report_ids.append(item["reportId"])
            assert item["status"] == expected and item["pid"] is None
            if scenario == "forged":
                assert item["reportId"] is None and "有效评测报告" in item["error"]
            else:
                assert item["reportId"] is not None
        finally:
            async with store.database.transaction() as tx:
                await tx.execute(
                    "DELETE FROM eval_repair_tickets WHERE run_id=ANY(%s)", (report_ids,)
                )
                await tx.execute("DELETE FROM eval_runs WHERE id=ANY(%s)", (report_ids,))

    asyncio.run(run())


def test_cancellation_stops_only_the_process_owned_by_this_worker(queue, tmp_path):  # noqa: F811
    script = tmp_path / "controlled_runner.py"
    script.write_text(SCRIPT)

    async def run():
        store, prefix = queue
        await enqueue(store, prefix, 1)
        claim = await store.claim()
        worker = EvaluationWorker(
            store.database,
            root=tmp_path,
            command_factory=lambda _claim, _report: [sys.executable, "-B", str(script), "wait"],
            heartbeat_ms=20,
        )
        task = asyncio.create_task(worker.execute(claim))
        pid = None
        for _ in range(50):
            row = await store.database.fetch_one(
                "SELECT pid FROM eval_queue_items WHERE id=%s", (claim["item"]["id"],)
            )
            if row["pid"]:
                pid = row["pid"]
                break
            await asyncio.sleep(0.02)
        assert pid is not None
        await store.cancel(claim["item"]["id"])
        await asyncio.wait_for(task, 5)
        row = await store.database.fetch_one(
            "SELECT status,pid,lease_token FROM eval_queue_items WHERE id=%s",
            (claim["item"]["id"],),
        )
        assert row == {"status": "cancelled", "pid": None, "lease_token": None}
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(run())


def test_real_python_worker_executes_native_benchmark_and_persists_attested_report(
    queue,  # noqa: F811
    tmp_path,
    monkeypatch,  # noqa: F811
):
    shutil.copytree(ROOT / "benchmarks/alphadeck", tmp_path / "benchmarks/alphadeck")
    store, prefix = queue
    monkeypatch.setenv("DATABASE_URL", store.database.url)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)

    async def run():
        item = await store.enqueue(
            {
                **virtual_item(
                    {"selectedCases": ["runtime-registry-deepseek-v4-flash"], "repeat": 2}
                ),
                "id": prefix + "native-cli",
            }
        )
        claim = await store.claim()
        assert claim["item"]["id"] == item["id"]
        worker = EvaluationWorker(store.database, root=tmp_path, heartbeat_ms=200)
        try:
            await asyncio.wait_for(worker.execute(claim), 30)
            row = await store.database.fetch_one(
                "SELECT status,report_id,error FROM eval_queue_items WHERE id=%s", (item["id"],)
            )
            assert row["status"] == "passed", row
            report = await worker.platform.run(row["report_id"])
            assert report["passed"] and report["qualitySummary"]["stability"]["attemptCount"] == 2
            assert report["metadata"]["queue"] == {
                "id": item["id"],
                "leaseToken": claim["leaseToken"],
            }
        finally:
            async with store.database.transaction() as tx:
                await tx.execute(
                    "DELETE FROM eval_runs WHERE metadata->'queue'->>'id'=%s", (item["id"],)
                )

    asyncio.run(run())
