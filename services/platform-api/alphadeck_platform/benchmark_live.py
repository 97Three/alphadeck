import asyncio
import base64
import os
import re
import time
from collections import Counter
from uuid import uuid4

import httpx

from .agent_executor import AgentExecutor
from .auth import Authorization
from .benchmark_artifacts import event_audit, inspect_artifacts
from .benchmark_visual import run_visual_check
from .build_identity import build_identity
from .dispatch import Dispatch
from .evaluation_attestation import attested
from .evaluation_reports import USAGE_FIELDS
from .evaluation_scoring import record
from .finance_generation import FinanceGenerationRuntime
from .generation_worker import GenerationWorker
from .lease_session import LeaseSession
from .llm_config import project_llm_config
from .prefetch_transport import WorkspaceArtifacts
from .runtime_store import snake
from .sessions import iso
from .workspace import write_atomic


def tools_summary(rows):
    counts = Counter(row["status"] for row in rows)
    return {
        "total": len(rows),
        "succeeded": counts["succeeded"],
        "failed": counts["failed"],
        "uncertain": counts["uncertain"],
        "unexpectedFailureCount": counts["failed"] + counts["uncertain"],
        "workspaceWriteSucceeded": sum(
            row["status"] == "succeeded" and row["effect"] == "workspace_write" for row in rows
        ),
        "submitResultSucceeded": sum(
            row["status"] == "succeeded" and row["tool_name"] == "submit_result" for row in rows
        ),
        "succeededToolNames": sorted(
            {row["tool_name"] for row in rows if row["status"] == "succeeded"}
        ),
    }


async def collect_execution(database, project, request, mission):
    rows = await database.fetch_all(
        "SELECT * FROM agent_runs WHERE project_id=%s AND (request_id=%s OR request_id LIKE %s) ORDER BY created_at,id",
        (project, request, request + "-validation-repair%"),
    )
    receipt = (
        await database.fetch_one(
            "SELECT * FROM agent_evidence_receipts WHERE id=%s", (mission["accepted_receipt_id"],)
        )
        if mission["accepted_receipt_id"]
        else None
    )
    candidate = await database.fetch_one(
        "SELECT * FROM agent_evidence_receipts WHERE mission_id=%s AND candidate_version=%s AND receipt_type='candidate' ORDER BY created_at DESC LIMIT 1",
        (mission["id"], mission["candidate_version"]),
    )
    runs, tools = [], []
    for row in rows:
        executions = await database.fetch_all(
            "SELECT * FROM agent_tool_executions WHERE run_id=%s ORDER BY prepared_at,id",
            (row["id"],),
        )
        tools.extend(executions)
        runs.append(
            {
                "id": row["id"],
                "runInstanceId": str(row["run_instance_id"]),
                "requestId": row["request_id"],
                "status": row["status"],
                "provider": row["provider"],
                "model": row["model"],
                "frameworkVersion": row["framework_version"],
                "buildRevision": row["build_revision"],
                "startedAt": iso(row["started_at"]) if row["started_at"] else None,
                "completedAt": iso(row["finished_at"]) if row["finished_at"] else None,
                "turns": row["turn_count"],
                "usage": {key: row[snake(key)] for key in USAGE_FIELDS.split()},
                "tools": tools_summary(executions),
            }
        )

    def only(key):
        values = list(dict.fromkeys(row[key] for row in rows))
        return values[0] if len(values) == 1 else None

    candidate_source = None
    if (
        candidate
        and receipt
        and candidate["source_run_id"] == receipt["source_run_id"]
        and candidate["source_request_id"] == receipt["source_request_id"]
    ):
        candidate_source = record(candidate["payload"]).get("source")
    execution = {
        "executed": bool(rows),
        "cli": "pi",
        "provider": only("provider"),
        "model": only("model"),
        "requestId": request,
        "runIds": [row["id"] for row in rows],
        "runs": runs,
        "missionId": mission["id"],
        "generationId": str(mission["generation_id"]),
        "missionStatus": mission["status"],
        "candidateVersion": mission["candidate_version"],
        "acceptedReceiptId": mission["accepted_receipt_id"],
        "acceptedReceiptHash": receipt["receipt_hash"] if receipt else None,
        "acceptedReceiptType": receipt["receipt_type"] if receipt else None,
        "acceptedReceiptVerdict": receipt["verdict"] if receipt else None,
        "acceptedSourceRunId": receipt["source_run_id"] if receipt else None,
        "acceptedSourceRequestId": receipt["source_request_id"] if receipt else None,
        "acceptedCandidateSource": candidate_source,
        "frameworkVersion": only("framework_version"),
        "buildRevision": only("build_revision"),
        "gitRevision": build_identity()["gitRevision"],
        "startedAt": min((run["startedAt"] for run in runs if run["startedAt"]), default=None),
        "completedAt": max(
            (run["completedAt"] for run in runs if run["completedAt"]), default=None
        ),
        "turns": sum(row["turn_count"] for row in rows),
        "usage": {key: sum(run["usage"][key] for run in runs) for key in USAGE_FIELDS.split()},
        "tools": {
            **tools_summary(tools),
            "errorCodes": dict(Counter(row["error_code"] for row in tools if row["error_code"])),
        },
    }
    acceptance = {
        key: execution[key]
        for key in (
            "missionId",
            "generationId",
            "candidateVersion",
            "acceptedReceiptId",
            "acceptedReceiptHash",
            "acceptedReceiptType",
            "acceptedReceiptVerdict",
            "acceptedSourceRunId",
            "acceptedSourceRequestId",
            "acceptedCandidateSource",
        )
    }
    acceptance["status"] = mission["status"]
    return execution, acceptance


async def execute_live_case(runner, case, options):
    from app import create_app

    from .benchmark_runner import IMAGE

    started = time.monotonic()
    project = (
        "benchmark-live-" + re.sub(r"[^a-zA-Z0-9_-]", "-", case["id"])[:50] + "-" + uuid4().hex[:8]
    )
    await runner.projects.create(
        {
            "projectId": project,
            "name": case.get("name") or case["id"],
            "initialPrompt": case["question"],
            "capabilityId": case.get("capabilityId"),
            "selectedModel": options["model"],
        }
    )
    runner.created.add(project)
    root = runner.projects.managed_path(await runner.projects.get(project), must_exist=True)
    request, images = "benchmark-" + uuid4().hex, []
    if case.get("imageAttachment"):
        write_atomic(root, "assets/benchmark-image.png", base64.b64decode(IMAGE))
        images = [
            {
                "path": "assets/benchmark-image.png",
                "name": case["imageAttachment"].get("filename", "portfolio-screenshot.png"),
                "mimeType": "image/png",
            }
        ]
    # This private ASGI invocation enters the same durable product route as the UI.
    # No externally accessible authentication bypass is created.
    application = create_app(
        runner.projects.database,
        Authorization(runner.projects.database, enabled=False, secret=""),
        projects_dir=runner.projects.root,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/api/chat/" + project + "/act",
            json={
                "instruction": case["question"],
                "requestId": request,
                "selectedModel": options["model"],
                "isInitialPrompt": True,
                "capabilityId": case.get("capabilityId"),
                "capabilitySelectionSource": "manual",
                "images": images,
            },
        )
    if response.status_code != 202:
        raise ValueError(
            "Product submission failed: " + str(response.status_code) + " " + response.text[:1000]
        )
    database = runner.projects.database
    runtime = runner.runtime or FinanceGenerationRuntime(
        database,
        runner.projects,
        runner.previews,
        transport=runner.transport,
        validator=runner.validator,
        executor=AgentExecutor(runner.projects, provider_factory=runner.provider)
        if runner.provider
        else None,
    )
    try:
        capacity = max(1, min(256, int(os.getenv("PI_AGENT_WORKER_GLOBAL_CONCURRENCY", "1"))))
    except ValueError:
        capacity = 1
    worker = GenerationWorker(database, runtime.registry(), global_concurrency=capacity)
    registration = await worker.registry.register(1, capacity, 120000)
    try:
        async with LeaseSession(
            lambda: worker.registry.heartbeat(registration, 120000),
            ttl_ms=120000,
            interval_ms=30000,
        ) as lease:
            job = await Dispatch(database).get(project, request)
            if not job:
                raise RuntimeError("Product submission did not create a durable generation job")
            async with asyncio.timeout(1800):
                while job["status"] in ("pending", "retry_wait", "running"):
                    if job["status"] != "running":
                        await lease.run(
                            lambda job=job: worker.execute_job(
                                job, "worker-instance:" + registration["id"]
                            )
                        )
                    await asyncio.sleep(0.1)
                    job = await Dispatch(database).get(project, request)
    finally:
        await worker.registry.stop(registration)
    mission = await database.fetch_one(
        "SELECT * FROM agent_missions WHERE project_id=%s AND request_id=%s", (project, request)
    )
    if not mission:
        raise RuntimeError("Product run did not create a Mission")
    execution, acceptance = await collect_execution(database, project, request, mission)
    artifacts = WorkspaceArtifacts(root, runner.authority)
    plan = artifacts.read(".data-agent/finance-run-plan.json") or {}
    validation = artifacts.read(".data-agent/validation.json") or {
        "status": "failed",
        "checks": [],
        "passed": False,
    }
    raw_files = [
        path.relative_to(root).as_posix()
        for path in (root / "data_file/raw").rglob("*.json")
        if path.is_file() and not path.is_symlink()
    ]
    prefetch = {
        "skipped": False,
        "rawFiles": raw_files,
        "finalDataPath": "data_file/final/dashboard-data.json",
        "symbols": plan.get("symbols", []),
    }
    inspection = await inspect_artifacts(root, case, prefetch)
    visual = await run_visual_check(runner, project, case)
    audit = event_audit(root, case)
    failures = [
        *inspection["failures"],
        *(visual["failures"] if visual else []),
        *audit["failures"],
        *(
            check["id"] + ": " + check["summary"]
            for check in validation["checks"]
            if check["status"] == "failed"
        ),
    ]
    if mission["status"] != "completed":
        failures.append("Mission 未通过独立验收：" + mission["status"])
    state = artifacts.read(".data-agent/generation-state.json") or {}
    result = {
        "id": case["id"],
        "name": case.get("name", case["id"]),
        "question": case["question"],
        "projectId": project,
        "projectPath": str(root),
        "requestId": request,
        "durationMs": round((time.monotonic() - started) * 1000),
        "passed": not failures,
        "failures": failures,
        "symbols": plan.get("symbols", []),
        "prefetch": prefetch,
        "artifacts": inspection,
        "eventAudit": audit,
        "visualCheck": visual if case.get("visualCheck") else None,
        "validation": validation,
        "repairAttempts": state.get("repairAttemptCount", 0),
        "platformRepairCount": 0,
        "agentExecuted": execution["executed"],
        "agentExecution": execution,
        "missionAcceptance": acceptance,
        "executionMode": "e2e",
    }
    config = project_llm_config(options["model"])
    if options["mode"] != "product-control" and not attested(
        result, {"provider": config["provider"], "model": config["model"]}
    ):
        failures.append("缺少可验真的模型运行、工具执行或 Mission 验收证据。")
        result["passed"] = False
    return result
