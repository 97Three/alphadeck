import asyncio
import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from alphadeck_platform.database import Database
from alphadeck_platform.mission_store import MissionError, Missions
from alphadeck_platform.provenance import provenance_hash, workspace_identity, workspace_snapshot


@pytest.fixture
def mission(database_url, actor):
    project = "mission-" + str(uuid4())
    request = str(uuid4())
    spec = json.loads((Path(__file__).parent / "fixtures/mission.json").read_text())["expected"]
    spec.update(projectId=project, requestId=request, runPlanId=request)
    spec["nodes"] = [
        {
            "key": key,
            "type": kind,
            "effect": "pure",
            "dependencies": [],
            "allowedTools": [],
            "requiredSkillSections": [],
            "inputArtifacts": [],
            "outputArtifacts": [],
            "budget": {
                "maxAttempts": 1,
                "maxToolCalls": 5,
                "maxInputTokens": 1000,
                "maxOutputTokens": 1000,
                "timeoutMs": 10000,
            },
            "acceptancePredicates": [],
        }
        for key, kind in (
            ("planning", "planner"),
            ("data_prefetch", "data"),
            ("workspace_generation", "writer"),
            ("validation", "validator"),
            ("evidence_verification", "verifier"),
            ("preview_readiness", "preview"),
        )
    ]
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Mission contract',%s,now())",
            (project, actor["user_id"]),
        )
        connection.execute(
            "INSERT INTO user_requests (id,project_id,actor_user_id,instruction) VALUES (%s,%s,%s,'test')",
            (request, project, actor["user_id"]),
        )
    store = Missions(Database(database_url))
    value = asyncio.run(store.ensure(spec))
    yield store, value, spec
    with psycopg.connect(database_url) as connection:
        connection.execute("DELETE FROM projects WHERE id=%s", (project,))


def candidate(ref, *, subject="a"):
    return {
        "schemaVersion": 1,
        "source": "platform_prefetch",
        "sourceRunId": None,
        "sourceRequestId": ref["requestId"],
        "workspaceSha256": "sha256:" + subject * 64,
        "summarySha256": "sha256:" + "b" * 64,
        "declaredArtifacts": ["app/page.tsx"],
        "verifiedArtifacts": ["app/page.tsx"],
        "submittedAt": "2026-11-01T00:00:00.000Z",
    }


def decision(ref, verdict="accepted", version=1):
    payload = {
        "schemaVersion": 1,
        "missionId": ref["id"],
        "generationId": ref["generationId"],
        "projectId": ref["projectId"],
        "requestId": ref["requestId"],
        "candidateVersion": version,
        "missionSpecSha256": ref["specHash"],
        "preview": {
            "url": "http://localhost:4100",
            "port": 4100,
            "httpStatus": 200,
            "responseSha256": "a" * 64,
            "readyAt": "2026-11-01T00:00:00.000Z",
        },
        "validation": {"checks": []},
        "decision": {"verdict": verdict, "reasonCodes": [], "failedCheckIds": []},
        "createdAt": "2026-11-01T00:00:00.000Z",
    }
    return {
        "verdict": verdict,
        "reasonCodes": [],
        "failedCheckIds": [],
        "candidateVersion": version,
        "subjectHash": "sha256:" + "a" * 64,
        "payload": payload,
        "receiptHash": "sha256:" + provenance_hash(payload),
    }


def test_provenance_matches_original_javascript_for_numbers_and_unicode():
    cases = json.loads((Path(__file__).parent / "fixtures/provenance.json").read_text())
    for case in cases:
        assert provenance_hash(case["input"]) == case["expected"]


def test_failed_acceptance_releases_project_for_a_new_request(database_url, mission):
    store, ref, spec = mission

    async def run():
        await store.candidate(ref, candidate(ref))
        fence = await store.begin_verification(ref, "failed-verifier")
        outcome = await store.evidence(ref, fence, decision(ref, "failed"))
        assert outcome["mission"]["status"] == "failed"
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT active_slot,completed_at IS NOT NULL FROM agent_missions WHERE id=%s",
                (ref["id"],),
            ).fetchone() == (None, True)
            assert (
                connection.execute(
                    "SELECT status FROM user_requests WHERE id=%s", (ref["requestId"],)
                ).fetchone()[0]
                == "failed"
            )
            request = str(uuid4())
            connection.execute(
                "INSERT INTO user_requests (id,project_id,instruction) VALUES (%s,%s,'new research')",
                (request, ref["projectId"]),
            )
        new_spec = {**spec, "requestId": request, "runPlanId": request}
        new = await store.ensure(new_spec)
        assert new["id"] != ref["id"] and new["status"] == "running"

    asyncio.run(run())


def test_mission_spec_candidate_verification_and_acceptance_are_separate_authorities(
    database_url, mission
):
    store, ref, spec = mission

    async def run():
        assert await store.ensure(spec) == ref
        with pytest.raises(MissionError) as changed:
            await store.ensure({**spec, "capabilityId": "other"})
        assert changed.value.code == "MISSION_SPEC_CONFLICT"
        first = await store.candidate(ref, candidate(ref))
        replay = await store.candidate(ref, candidate(ref))
        assert first == replay and first["mission"]["status"] == "candidate_complete"
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM user_requests WHERE id=%s", (ref["requestId"],)
                ).fetchone()[0]
                == "pending"
            )
        claim = await store.begin_verification(ref, "verifier")
        with pytest.raises(MissionError) as busy:
            await store.begin_verification(ref, "other-verifier")
        assert busy.value.code == "MISSION_VERIFICATION_BUSY"
        await store.heartbeat(ref, claim)
        result = await store.evidence(ref, claim, decision(ref))
        assert result["mission"]["status"] == "completed"
        assert await store.evidence(ref, claim, decision(ref)) == result
        snapshot = await store.accepted_snapshot(ref["projectId"], ref["requestId"])
        assert (
            snapshot["previewPort"] == 4100
            and snapshot["acceptedReceiptId"] == result["receipt"]["id"]
        )
        assert (await store.terminal(ref, "cancelled", "MISSION_CANCELLED", "too late"))[
            "status"
        ] == "completed"
        with pytest.raises(MissionError):
            await store.candidate(ref, candidate(ref, subject="c"))
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM user_requests WHERE id=%s", (ref["requestId"],)
                ).fetchone()[0]
                == "completed"
            )
            assert (
                connection.execute(
                    "SELECT active_slot FROM agent_missions WHERE id=%s", (ref["id"],)
                ).fetchone()[0]
                is None
            )

    asyncio.run(run())


def test_expired_verifier_can_be_replaced_and_old_fence_cannot_publish(database_url, mission):
    store, ref, _ = mission

    async def run():
        await store.candidate(ref, candidate(ref))
        old = await store.begin_verification(ref, "old-verifier")
        with psycopg.connect(database_url) as connection:
            connection.execute(
                "UPDATE agent_missions SET verification_lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (ref["id"],),
            )
        new = await store.begin_verification(ref, "new-verifier")
        assert new["fencingToken"] > old["fencingToken"]
        with pytest.raises(MissionError) as stale:
            await store.evidence(ref, old, decision(ref))
        assert stale.value.code == "MISSION_VERIFICATION_LEASE_LOST"
        invalid = decision(ref, version=0)
        with pytest.raises(MissionError) as previous:
            await store.evidence(ref, new, invalid)
        assert previous.value.code == "EVIDENCE_CANDIDATE_STALE"
        await store.abandon(ref, new)
        claim = await store.begin_verification(ref, "third-verifier")
        repair = await store.evidence(ref, claim, decision(ref, "repair_required"))
        assert repair["mission"]["status"] == "repair_required"
        await store.repairing(ref)
        replacement = candidate(ref, subject="c")
        replacement["sourceRequestId"] += "-validation-repair-1"
        result = await store.candidate(ref, replacement)
        assert result["mission"]["candidateVersion"] == 2

    asyncio.run(run())


def test_request_cancellation_wins_without_leaking_acceptance_receipt(database_url, mission):
    store, ref, _ = mission

    async def run():
        await store.candidate(ref, candidate(ref))
        claim = await store.begin_verification(ref, "verifier")
        with psycopg.connect(database_url) as connection:
            connection.execute(
                "UPDATE user_requests SET status='cancelled',completed_at=now() WHERE id=%s",
                (ref["requestId"],),
            )
        with pytest.raises(MissionError) as cancelled:
            await store.evidence(ref, claim, decision(ref))
        assert cancelled.value.code == "MISSION_REQUEST_NOT_ACTIVE"
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM agent_evidence_receipts WHERE mission_id=%s AND receipt_type='acceptance'",
                    (ref["id"],),
                ).fetchone()[0]
                == 0
            )
        result = await store.terminal(ref, "cancelled", "MISSION_CANCELLED", "User cancelled")
        assert result["status"] == "cancelled"

    asyncio.run(run())


def test_workspace_snapshot_ignores_runtime_locks_and_follows_no_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app").mkdir()
    (workspace / "app/page.tsx").write_text("hello")
    before = workspace_snapshot(workspace)
    (workspace / ".pi-workspace.lock").mkdir()
    (workspace / ".pi-workspace.lock/owner.json").write_text("internal")
    assert workspace_snapshot(workspace) == before
    (workspace / "alias").symlink_to("/missing")
    after = workspace_snapshot(workspace)
    assert after["fileCount"] == 2 and after["hashedBytes"] == 5
    assert workspace_identity(workspace) == workspace_identity(workspace / "app/..")
    assert workspace_identity(workspace, namespace="different") != workspace_identity(workspace)
