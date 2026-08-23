import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest
from test_run_engine import engine_context  # noqa: F401

from alphadeck_platform.mission_control import (
    MissionControl,
    capture_candidate,
    finance_definition,
)
from alphadeck_platform.mission_store import MissionError, Missions

FIXTURES = Path(__file__).parent / "fixtures"
CASES = json.loads((FIXTURES / "finance-mission.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_finance_mission_definition_matches_original(case):
    expected = copy.deepcopy(case["expected"])
    # The frozen transport artifact is now the platform-owned Vite bridge.
    for artifact in expected["artifacts"]:
        if artifact["path"] == "app/api/market/[...path]/route.ts":
            artifact["path"] = "vite.config.ts"
    seen_vite = False
    retained = []
    for artifact in expected["artifacts"]:
        if artifact["path"] == "vite.config.ts":
            if seen_vite:
                continue
            seen_vite = True
        retained.append(artifact)
    expected["artifacts"] = retained
    assert (
        finance_definition(case["input"]["maxRepairAttempts"], case["input"]["expectedArtifacts"])
        == expected
    )


def test_claim_verify_and_commit_acceptance_before_workspace_unlock(engine_context):  # noqa: F811
    database, input, _, root = engine_context
    case = next(
        value
        for value in json.loads((FIXTURES / "evidence.json").read_text())
        if value["expected"]["verdict"] == "accepted"
    )
    spec = case["input"]["missionSpec"]
    spec["nodes"] = finance_definition(0)["nodes"]
    spec.update(
        projectId=input["projectId"], requestId=input["requestId"], runPlanId=input["requestId"]
    )
    for relative, content in case["files"].items():
        if relative == spec["validationReportPath"]:
            report = json.loads(content)
            report.update(projectId=input["projectId"], runId=input["requestId"])
            content = json.dumps(report)
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / "src").mkdir(exist_ok=True)

    async def run():
        store = Missions(database)
        mission = await store.ensure(spec)
        sealed = await store.candidate(
            mission,
            capture_candidate(
                root, source="platform_prefetch", source_request_id=input["requestId"]
            ),
        )
        control = MissionControl(database)
        async with control.verification(sealed["mission"], root) as session:
            with pytest.raises(MissionError) as busy:
                await store.begin_verification(mission, "competing-verifier")
            assert busy.value.code == "MISSION_VERIFICATION_BUSY"

            def response(_):
                assert (root / ".pi-workspace.lock").is_dir()
                return httpx.Response(200, content=b"<html>verified</html>")

            async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
                result = await session.verify(case["input"]["preview"], client=client)
            assert result["decision"]["verdict"] == "accepted", result["decision"]["reasonCodes"]
            assert result["mission"]["status"] == "completed"
            assert result["mission"]["acceptedReceiptId"] == result["receipt"]["id"]
        assert not (root / ".pi-workspace.lock").exists()
        assert (await store.read(input["projectId"], input["requestId"]))["status"] == "completed"

    asyncio.run(run())
