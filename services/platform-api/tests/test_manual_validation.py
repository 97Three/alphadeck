import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_run_engine import engine_context  # noqa: F401

from alphadeck_platform.auth import Authorization
from alphadeck_platform.generation_state import GenerationState
from alphadeck_platform.mission_control import capture_candidate, finance_definition
from alphadeck_platform.mission_store import Missions
from alphadeck_platform.projects import Projects
from app import create_app


@pytest.mark.parametrize(
    "scenario",
    ["candidate", "expired-verification", "live-verification", "running", "failed", "legacy"],
)
def test_manual_validation_guards_identity_terminal_state_and_exclusive_verification(
    engine_context,  # noqa: F811
    actor,
    scenario,
):
    database, identity, _, temporary = engine_context
    project, request = identity["projectId"], identity["requestId"]
    root = temporary / project
    root.mkdir()
    case = json.loads((Path(__file__).parent / "fixtures/evidence.json").read_text())[0]
    spec = case["input"]["missionSpec"]
    spec.update(projectId=project, requestId=request, runPlanId=request)
    spec["nodes"] = finance_definition(1)["nodes"]
    for relative, content in case["files"].items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    async def run():
        async with database.transaction() as tx:
            await tx.execute("UPDATE projects SET repo_path=%s WHERE id=%s", (str(root), project))
        mission = await Missions(database).ensure(spec) if scenario != "legacy" else None
        if mission and scenario != "running":
            sealed = await Missions(database).candidate(
                mission,
                capture_candidate(root, source="workspace_recovery", source_request_id=request),
            )
            mission = sealed["mission"]
            if scenario == "failed":
                mission = await Missions(database).terminal(
                    mission, "failed", "FIXTURE_FAILED", "Test terminal"
                )
            elif scenario in {"expired-verification", "live-verification"}:
                await Missions(database).begin_verification(mission, "other-owner")
                if scenario == "expired-verification":
                    async with database.transaction() as tx:
                        await tx.execute(
                            "UPDATE agent_missions SET verification_lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                            (mission["id"],),
                        )

        async def authority(operation):
            return await operation(None)

        await GenerationState(root, authority=authority).start(project, request, "research")
        calls = []

        class Validator:
            transport = None

            async def validate(self, project_id, artifacts, request_id, *, prepared=False):
                calls.append(request_id)
                report = {
                    "schemaVersion": 1,
                    "projectId": project_id,
                    "runId": request_id,
                    "passed": True,
                    "status": "passed",
                    "checks": [
                        {
                            "id": key,
                            "status": "passed",
                            "name": key,
                            "summary": "Controlled validator result",
                        }
                        for key in spec["requiredValidationCheckIds"]
                    ],
                }
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
                projects = Projects(database, temporary)

                async def start(self, selected):
                    return {"url": f"http://127.0.0.1:{port}", "port": port, "status": "running"}

                async def stop(self, selected):
                    return None

            app = create_app(
                database,
                Authorization(database, enabled=True, secret="test-secret"),
                projects_dir=temporary,
                preview_manager=Previews(),
                validator_factory=lambda _: Validator(),
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                cookies={"alphadeck.session_token": actor["cookie"]},
            ) as client:
                path = f"/api/projects/{project}/quant/validation"
                unknown = await client.post(path, json={"requestId": "does-not-exist"})
                assert unknown.status_code == 404 and not calls
                response = await client.post(path, json={"requestId": request})
                if scenario in {"running", "failed", "live-verification"}:
                    assert response.status_code == 409, response.text
                    assert not calls
                    code = response.json()["code"]
                    assert (
                        code
                        == {
                            "running": "MISSION_BUSY",
                            "failed": "MISSION_TERMINAL",
                            "live-verification": "MISSION_VERIFICATION_BUSY",
                        }[scenario]
                    )
                else:
                    assert response.status_code == 200, response.text
                    result = response.json()
                    assert result["data"]["passed"] is True and result["preview"]["port"] == port
                    if scenario != "legacy":
                        assert result["acceptance"]["satisfied"] is True
                        current = await Missions(database).accepted_snapshot(project, request)
                        assert current["acceptedReceiptId"] == result["acceptance"]["receiptId"]
                        final_page = (root / "src/App.vue").read_bytes()
                        again = await client.post(path, json={"requestId": request})
                        assert (
                            again.status_code == 409
                            and again.json()["code"] == "MISSION_REVALIDATION_REQUIRES_NEW_REQUEST"
                        )
                        assert (root / "src/App.vue").read_bytes() == final_page
                        assert len(calls) == 1
                loaded = await client.get(path)
                assert (
                    loaded.status_code == 200
                    and loaded.json()["generationState"]["requestId"] == request
                )

    asyncio.run(run())
