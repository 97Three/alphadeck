import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import psycopg
import pytest

from alphadeck_platform.approvals import ApprovalError, Approvals
from alphadeck_platform.auth import Authorization
from alphadeck_platform.database import Database
from alphadeck_platform.runtime_store import RuntimeStore, fence_for
from app import create_app


def test_approval_scope_atomic_resolution_and_private_timeline(database_url, actor, tmp_path):
    project = "approval-test-" + str(uuid4())
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Approval test',%s,now())",
            (project, actor["user_id"]),
        )
    database = Database(database_url)
    runs = RuntimeStore(database)
    approvals = Approvals(database)
    app = create_app(
        database, Authorization(database, enabled=True, secret="test-secret"), tmp_path
    )

    async def run():
        row = await runs.create(
            {
                "id": "run-" + str(uuid4()),
                "projectId": project,
                "workspaceKey": "sha256:" + "a" * 64,
                "provider": "openai",
                "model": "fixture",
                "frameworkVersion": "1.0.0",
                "buildRevision": "python",
                **{
                    key: "a" * 64
                    for key in (
                        "profileHash",
                        "promptHash",
                        "toolHash",
                        "skillHash",
                        "workspaceHash",
                    )
                },
            },
            owner="worker",
        )
        at = datetime.now(UTC).replace(microsecond=0)
        result = await runs.append(
            fence_for(row),
            "approval-wait-" + str(uuid4()),
            1,
            "approval_requested",
            {"phase": "write"},
            at,
            transition="waiting",
        )
        row = result["run"]
        identity = "approval_" + uuid4().hex
        requested = int(at.timestamp() * 1000)
        request = {
            "approvalId": identity,
            "runId": row["id"],
            "turn": 1,
            "toolCallId": "call-1",
            "toolName": "write_file",
            "effect": "workspace_write",
            "idempotency": "reconcile_required",
            "inputSha256": "a" * 64,
            "publicInput": {"path": "app/page.tsx"},
            "reason": "Workspace mutation",
            "allowedDecisions": ["approve", "edit", "reject"],
            "requestedAt": requested,
            "expiresAt": requested + 60000,
        }
        await approvals.create(request)
        await approvals.create(request)
        with pytest.raises(ApprovalError) as collision:
            await approvals.create({**request, "publicInput": {"path": "different"}})
        assert collision.value.code == "APPROVAL_CONFLICT"
        with pytest.raises(ApprovalError) as cross:
            await approvals.resolve(
                "another-project",
                identity,
                actor["user_id"],
                "approve",
                actor_user=actor["user_id"],
            )
        assert cross.value.code == "APPROVAL_NOT_FOUND"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"alphadeck.session_token": actor["cookie"]},
        ) as client:
            path = f"/api/projects/{project}/agent/approvals"
            listing = await client.get(path)
            assert listing.status_code == 200, listing.text
            assert listing.json()["data"][0]["id"] == identity
            results = await asyncio.gather(
                client.post(path + "/" + identity, json={"decision": "approve"}),
                client.post(path + "/" + identity, json={"decision": "reject"}),
            )
            assert sorted(item.status_code for item in results) == [200, 409]
            resolution = await approvals.wait(project, identity)
            assert (
                resolution["decision"] in {"approve", "reject"}
                and resolution["resolvedBy"] == actor["user_id"]
            )
            timeline = await client.get(
                f"/api/projects/{project}/agent/runs/{row['id']}", params={"limit": 1}
            )
            assert timeline.status_code == 200, timeline.text
            data = timeline.json()["data"]
            assert len(data["events"]) == 1 and len(data["toolApprovals"]) == 1
            assert (
                "promptHash" not in data
                and "workspaceKey" not in data
                and "errorMessage" not in data
            )
            assert "opaqueState" not in timeline.text
        # Expiration must commit even though the resolver returns an error.
        expired = {
            **request,
            "approvalId": "approval_" + uuid4().hex,
            "requestedAt": requested - 120000,
            "expiresAt": requested - 60000,
        }
        await approvals.create(expired)
        with pytest.raises(ApprovalError) as timeout:
            await approvals.resolve(
                project,
                expired["approvalId"],
                actor["user_id"],
                "approve",
                actor_user=actor["user_id"],
            )
        assert timeout.value.code == "APPROVAL_EXPIRED"
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM agent_tool_approvals WHERE id=%s", (expired["approvalId"],)
                ).fetchone()[0]
                == "expired"
            )

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM projects WHERE id=%s", (project,))
