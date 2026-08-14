import asyncio
import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_memory_service import runtime as memory_runtime
from test_run_engine import engine_context  # noqa: F401

from alphadeck_platform.agent_executor import AgentExecutor
from alphadeck_platform.dispatch import Dispatch
from alphadeck_platform.finance_catalog import FinanceCatalog
from alphadeck_platform.finance_generation import FinanceGenerationRuntime
from alphadeck_platform.generation_envelope import create_finance_envelope
from alphadeck_platform.generation_worker import GenerationWorker
from alphadeck_platform.integration_config import integration_scope
from alphadeck_platform.knowledge_service import KnowledgeService
from alphadeck_platform.memory_service import MemoryService
from alphadeck_platform.mission_store import Missions
from alphadeck_platform.projects import Projects

FIXTURES = Path(__file__).parent / "fixtures"
PLANNING = json.loads((FIXTURES / "finance-planning.json").read_text())["cases"]
DATA = next(
    item
    for item in json.loads((FIXTURES / "agent-prompts.json").read_text())["assessments"]
    if item["name"] == "singleStockData_valid:valid"
)


@pytest.mark.parametrize(
    "scenario",
    ["stock", "missing_target", "return_promise", "prefetch-failure", "cancel", "resume", "memory"],
)
def test_native_production_worker_from_durable_question_to_accepted_preview(
    engine_context,  # noqa: F811
    monkeypatch,
    scenario,
):
    database, identity, _, temporary = engine_context
    project, request = identity["projectId"], identity["requestId"]
    root = temporary / project
    root.mkdir()
    monkeypatch.setenv("ALPHADECK_MEMORY_ENABLED", "0")
    monkeypatch.setenv("ALPHADECK_KNOWLEDGE_ENABLED", "0")
    monkeypatch.setenv("PI_AGENT_DISPATCH_MODE", "worker")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODELPORT_API_KEY", raising=False)
    case = next(
        item
        for item in PLANNING
        if item["name"]
        == (scenario if scenario in {"missing_target", "return_promise"} else "stock")
    )
    application = FinanceCatalog().resolve()
    (root / ".data-agent").mkdir()
    (root / ".data-agent/workspace.json").write_text(
        json.dumps(
            {"schemaVersion": 1, "projectId": project, "composition": application["composition"]}
        )
    )
    instruction = case["params"]["instruction"]

    async def run():
        async with database.transaction() as tx:
            actor_row = await (
                await tx.execute("SELECT actor_user_id FROM user_requests WHERE id=%s", (request,))
            ).fetchone()
            actor = actor_row["actor_user_id"]
            await tx.execute(
                "UPDATE projects SET repo_path=%s,agent_profile_id=%s,agent_profile_version=%s,data_agent_composition_sha256=%s WHERE id=%s",
                (
                    str(root),
                    application["profile"]["id"],
                    application["profile"]["version"],
                    application["composition"]["sha256"],
                    project,
                ),
            )
        memory, memory_port = (
            memory_runtime(database, project)
            if scenario == "memory"
            else (MemoryService(database), None)
        )
        scope_factory = memory.scope if memory_port else integration_scope
        scope = scope_factory(project)
        if memory_port:
            await memory.repository.set_control(actor, True)
        payload = {
            "phase": "preparation",
            "schemaVersion": 1,
            "finalInstruction": instruction,
            "displayInstruction": instruction,
            "imageAttachmentInstruction": "",
            "selectedModel": "deepseek-v4-flash",
            "cliPreference": "pi",
            "isInitialPrompt": True,
            "conversationId": None,
            "actorUserId": actor,
            "memorySubjectId": actor,
            "userMessageId": "user-" + request,
            "capabilityId": "stock_diagnosis"
            if scenario not in {"missing_target", "return_promise"}
            else None,
            "capabilitySelectionSource": "manual"
            if scenario not in {"missing_target", "return_promise"}
            else None,
            "processedImages": [],
        }
        envelope = create_finance_envelope(
            payload, project, request, application["capability"]["id"], scope=scope
        )
        job = await Dispatch(database).enqueue(
            project,
            request,
            instruction,
            execution_envelope=envelope,
            model="deepseek-v4-flash",
            cli="pi",
            stage="planning_data_prefetch",
        )
        calls, prefetches = [], []

        async def rewrite(query, options):
            calls.append(query)
            return copy.deepcopy(case["params"]["queryRewrite"])

        async def prefetch(plan, transport, artifacts, **options):
            prefetches.append(plan["runId"])
            if scenario == "prefetch-failure":
                raise RuntimeError("所有标的预取失败：隔离测试行情源返回 HTTP 502")
            if scenario == "cancel":
                from alphadeck_platform.user_requests import UserRequests

                await UserRequests(database).cancel(project, request)
                await options["assert_active"]()
            for path, key in (
                ("data_file/final/dashboard-data.json", "final"),
                ("evidence/sources.json", "sources"),
                ("evidence/data_quality.json", "quality"),
            ):
                value = json.loads(json.dumps(DATA[key]).replace("600589", "600519"))
                value["runId"] = request
                await artifacts.write(path, value)
            from alphadeck_platform.prepared_artifacts import assess_prepared

            assessment = assess_prepared(artifacts.root, plan)
            assert assessment["ready"], assessment
            return {
                "skipped": False,
                "summary": "测试行情源已取得真实结构的数据。",
                "rawFiles": [],
                "symbols": ["600519"],
            }

        class Validator:
            transport = None

            async def validate(self, selected_project, artifacts, selected_request, *, prepared):
                assert (selected_project, selected_request, prepared) == (project, request, True)
                report = {
                    "projectId": project,
                    "runId": request,
                    "passed": True,
                    "status": "passed",
                    "checks": [
                        {
                            "id": check,
                            "name": check,
                            "status": "passed",
                            "summary": "Scripted validation; real build and screenshots covered separately",
                        }
                        for check in json.loads(
                            (
                                Path(__file__).parents[3] / "config/finance-mission-definition.json"
                            ).read_text()
                        )["definition"]["requiredValidationCheckIds"]
                    ],
                }
                for relative in (
                    ".data-agent/visual-validation.json",
                    ".data-agent/artifact-contracts.json",
                ):
                    await artifacts.write(
                        relative, {"schemaVersion": 1, "runId": request, "passed": True}
                    )
                await artifacts.write(".data-agent/validation.json", report)
                return report

        async def http(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(http, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            class Previews:
                async def start(self, _project):
                    return {"url": f"http://127.0.0.1:{port}", "port": port, "status": "running"}

            projects = Projects(database, temporary)
            executor = AgentExecutor(
                projects,
                provider_factory=lambda _: pytest.fail(
                    "Prepared standard lane must never contact a model"
                ),
            )
            runtime = FinanceGenerationRuntime(
                database,
                projects,
                Previews(),
                executor=executor,
                validator=Validator(),
                memory=memory,
                scope_factory=scope_factory,
                knowledge=KnowledgeService(),
                rewriter=rewrite,
                prefetcher=prefetch,
            )
            worker = GenerationWorker(
                database, runtime.registry(), pool="finance.test." + uuid4().hex
            )
            if scenario == "resume":

                class InterruptedExecutor:
                    async def execute(self, *args, **kwargs):
                        raise asyncio.CancelledError()

                runtime.executor = InterruptedExecutor()
                with pytest.raises(asyncio.CancelledError):
                    await worker.execute_job(job, "first-owner")
                prepared_job = await Dispatch(database).get(project, request)
                assert prepared_job["stage"] == "agent_execution"
                assert prepared_job["execution_envelope"]["payload"]["missionId"]
                async with database.transaction() as tx:
                    await tx.execute(
                        "UPDATE agent_generation_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                        (job["id"],),
                    )
                await Dispatch(database).reconcile()
                async with database.transaction() as tx:
                    await tx.execute(
                        "UPDATE agent_generation_jobs SET available_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                        (job["id"],),
                    )
                runtime.executor = executor
            await worker.execute_job(job, "second-owner")
        final = await Dispatch(database).get(project, request)
        mission = await Missions(database).read(project, request)
        assert len(calls) == 1, "Prepared-input retry must not repeat query rewriting"
        if scenario in {"stock", "resume", "memory"}:
            assert final["status"] == "completed", final.get("error_message")
            snapshot = await Missions(database).accepted_snapshot(project, request)
            assert snapshot["missionStatus"] == "completed" and snapshot["acceptedReceiptId"]
            assert snapshot["previewPort"] == port
            assert (root / "src/App.vue").is_file()
            assert len(prefetches) == 1
            runs = await database.fetch_all(
                "SELECT status,total_tokens FROM agent_runs WHERE project_id=%s", (project,)
            )
            assert runs == [{"status": "candidate_complete", "total_tokens": 0}]
            context_path = root / f"evidence/context-uses/{request}.json"
            if memory_port:
                manifest = json.loads(context_path.read_text())
                assert (
                    manifest["acceptedMission"]["acceptedReceiptId"]
                    == snapshot["acceptedReceiptId"]
                )
                assert manifest["memory"]["usageId"] == memory_port.usage
                async with database.transaction() as tx:
                    await tx.execute(
                        "DELETE FROM personal_memory_controls WHERE subject_id=%s", (actor,)
                    )
            else:
                assert not context_path.exists()
        elif scenario == "cancel":
            assert final["status"] == "cancelled"
            assert not (root / "data_file/final/dashboard-data.json").exists()
        elif scenario == "prefetch-failure":
            assert final["status"] == "failed" and mission["status"] == "failed"
            assert "HTTP 502" in final["error_message"]
        else:
            assert final["status"] == "completed" and mission is None
            assert not prefetches
            messages = await database.fetch_all(
                "SELECT metadata_json FROM messages WHERE request_id=%s", (request,)
            )
            assert any(
                json.loads(row["metadata_json"] or "{}").get("isMissionFinal") for row in messages
            )
        async with database.transaction() as tx:
            await tx.execute("DELETE FROM agent_worker_slots WHERE pool_key=%s", (worker.pool,))

    asyncio.run(run())
