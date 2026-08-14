import asyncio
import json
from pathlib import Path

import pytest
from test_run_engine import engine_context  # noqa: F401

from alphadeck_platform.agent_executor import AgentExecutionError
from alphadeck_platform.generation_state import GenerationState
from alphadeck_platform.generation_validation import GenerationValidation
from alphadeck_platform.mission_control import capture_candidate, finance_definition
from alphadeck_platform.mission_store import Missions
from alphadeck_platform.prefetch_transport import WorkspaceArtifacts


@pytest.mark.parametrize(
    "scenario", ["success", "repair", "exhausted", "unrepairable", "projection-recovery"]
)
def test_generation_validation_uses_committed_receipts_for_completion(
    engine_context,  # noqa: F811
    scenario,
    monkeypatch,
):
    database, input, _, root = engine_context
    case = json.loads((Path(__file__).parent / "fixtures/evidence.json").read_text())[0]
    spec = case["input"]["missionSpec"]
    spec.update(
        projectId=input["projectId"], requestId=input["requestId"], runPlanId=input["requestId"]
    )
    spec["nodes"] = finance_definition(1)["nodes"]
    # Use the actual native preparation and sealing, with a scripted build result.
    # The existing validation-runtime suite separately builds and screenshots real Next.js.
    for relative, content in case["files"].items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    async def run():
        async def authority(operation):
            return await operation(None)

        artifacts = WorkspaceArtifacts(root, authority)
        state = GenerationState(root, authority=authority)
        await state.start(input["projectId"], input["requestId"], "research", maximum=1)
        mission = await Missions(database).ensure(spec)
        validation_calls, repair_calls, finishes = [], [], []

        class Validator:
            transport = None

            async def validate(self, project, artifacts, request, *, prepared=False):
                assert prepared
                validation_calls.append(True)
                passed = (
                    scenario not in {"repair", "exhausted"}
                    or scenario == "repair"
                    and bool(repair_calls)
                )
                report = {
                    "reportPath": ".data-agent/validation.json",
                    "projectId": project,
                    "runId": request,
                    "passed": passed,
                    "status": "passed" if passed else "failed",
                    "checks": [
                        {
                            "id": "vue_build",
                            "name": "Vue/Vite build",
                            "status": "passed" if passed else "failed",
                            "summary": "Build complete" if passed else "TypeScript error",
                        },
                        {
                            "id": "evidence_files",
                            "name": "Evidence",
                            "status": "passed",
                            "summary": "Evidence complete",
                        },
                    ],
                }
                await artifacts.write(".data-agent/validation.json", report)
                return report

        class Executor:
            async def execute(self, project, instruction, request, **options):
                repair_calls.append(options)
                assert options["parent_request"] == input["requestId"]
                assert options["profile"] == "repair"
                if scenario == "exhausted":
                    raise AgentExecutionError("PROVIDER_TIMEOUT", "Model unavailable")
                return capture_candidate(
                    root, source="pi_agent_submit_result", source_request_id=request
                )

        async def http(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(http, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            class Previews:
                async def start(self, project):
                    return {"url": f"http://127.0.0.1:{port}", "port": port, "status": "running"}

            async def finish(project, request, status, **options):
                finishes.append(status)
                if status == "completed":
                    snapshot = await Missions(database).accepted_snapshot(project, request)
                    assert (
                        snapshot["acceptedReceiptId"] and snapshot["missionStatus"] == "completed"
                    )

            async def execute():
                if scenario == "unrepairable":
                    raise AgentExecutionError("MODEL_UNCONFIGURED", "No credentials")
                return capture_candidate(
                    root, source="platform_prefetch", source_request_id=input["requestId"]
                )

            if scenario == "projection-recovery":
                real_update = GenerationState.update
                failed_once = []

                async def update(self, project, request, step, *args, **options):
                    if step == "completed" and not failed_once:
                        failed_once.append(True)
                        raise OSError(
                            "Interrupted progress projection after accepted receipt commit"
                        )
                    return await real_update(self, project, request, step, *args, **options)

                monkeypatch.setattr(GenerationState, "update", update)
            result = await GenerationValidation(
                database, Validator(), Previews(), Executor(), finish=finish
            ).run(
                input["projectId"],
                input["requestId"],
                mission,
                artifacts,
                "research",
                "deepseek-v4-flash",
                execute(),
            )
        if scenario in {"success", "repair", "projection-recovery"}:
            assert result["status"] == "completed", result
            assert state.read()["status"] == "completed"
            assert finishes == ["completed"]
            if scenario == "repair":
                assert len(repair_calls) == 1 and len(validation_calls) == 2
        else:
            assert result["status"] == "failed", result
            assert finishes == ["failed"]
            if scenario == "unrepairable":
                assert not validation_calls and not repair_calls
            if scenario == "exhausted":
                assert len(repair_calls) == 1 and len(validation_calls) == 2

    asyncio.run(run())
