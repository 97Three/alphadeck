import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest

from alphadeck_platform.database import Database
from alphadeck_platform.runtime_policy import RuntimeStoreError, public_json
from alphadeck_platform.runtime_store import RuntimeStore, fence_for


@pytest.fixture
def runtime(database_url, actor):
    project = "runtime-" + str(uuid4())
    request = str(uuid4())
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Runtime contract',%s,now())",
            (project, actor["user_id"]),
        )
        connection.execute(
            "INSERT INTO user_requests (id,project_id,actor_user_id,instruction) VALUES (%s,%s,%s,'test')",
            (request, project, actor["user_id"]),
        )
    provenance = {
        "id": "run-" + str(uuid4()),
        "projectId": project,
        "requestId": request,
        "workspaceKey": "sha256:" + uuid4().hex + uuid4().hex,
        "provider": "openai",
        "model": "test-model",
        "frameworkVersion": "1.0.0",
        "buildRevision": "python-migration",
        **{
            key: "a" * 64
            for key in ("profileHash", "promptHash", "toolHash", "skillHash", "workspaceHash")
        },
    }
    yield RuntimeStore(Database(database_url)), provenance
    with psycopg.connect(database_url) as connection:
        connection.execute("DELETE FROM projects WHERE id=%s", (project,))


def test_events_checkpoints_and_usage_are_versioned_and_private_state_is_rejected(runtime):
    store, input = runtime

    async def run():
        initial = await store.create(input, owner="worker")
        assert initial["fencing_token"] == 1 and initial["status"] == "running"
        fence = fence_for(initial)
        at = datetime.now(UTC).replace(microsecond=0)
        result = await store.append(fence, "event-1", 1, "run_started", {"phase": "research"}, at)
        current = result["run"]
        replay = await store.append(fence, "event-1", 1, "run_started", {"phase": "research"}, at)
        assert replay["run"]["version"] == current["version"]
        with pytest.raises(RuntimeStoreError) as conflict:
            await store.append(fence, "event-1", 1, "run_started", {"phase": "other"}, at)
        assert conflict.value.code == "CONFLICT"
        with pytest.raises(RuntimeStoreError) as secret:
            await store.checkpoint(
                fence_for(current), 1, 0, "run_started", {"reasoning_content": "private"}, "a" * 64
            )
        assert secret.value.code == "INVALID_STATE"
        result = await store.checkpoint(
            fence_for(current),
            1,
            0,
            "run_started",
            {"phase": "research", "completedTools": []},
            "a" * 64,
        )
        current = result["run"]
        with pytest.raises(RuntimeStoreError) as stale:
            await store.heartbeat(fence)
        assert stale.value.code == "CONFLICT"
        result = await store.append(
            fence_for(current),
            "event-2",
            2,
            "approval_requested",
            {"approvalId": "approval-1"},
            at,
            transition="waiting",
        )
        current = result["run"]
        assert current["status"] == "waiting"
        result = await store.append(
            fence_for(current),
            "event-3",
            3,
            "approval_resolved",
            {"decision": "approve"},
            at,
            transition="running",
            cumulative_usage={"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        )
        current = result["run"]
        finished = await store.complete(
            fence_for(current),
            "candidate_complete",
            1,
            {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        )
        assert finished["status"] == "candidate_complete"
        assert len(await store.events(initial["id"])) == 3
        assert (await store.latest_checkpoint(initial["id"]))["recovery_mode"] == "replan_required"
        with pytest.raises(RuntimeStoreError) as no_replay:
            await store.append(fence, "event-1", 1, "run_started", {"phase": "research"}, at)
        assert no_replay.value.code == "LEASE_LOST"
        with pytest.raises(RuntimeStoreError) as terminal:
            await store.claim(initial["id"], "another", finished["version"])
        assert terminal.value.code == "INVALID_STATE"

    asyncio.run(run())


def test_tool_mutation_commits_once_and_completion_receipt_replays_without_executing(runtime):
    store, input = runtime
    calls = []

    async def run():
        current = await store.create(input, owner="worker")
        prepared = await store.prepare_tool(
            fence_for(current),
            "operation-1",
            "call-1",
            "write_file",
            "a" * 64,
            "workspace_write",
            "reconcile_required",
            pre_state_hash="b" * 64,
        )
        current = prepared["run"]
        repeated = await store.prepare_tool(
            fence_for(current),
            "operation-1",
            "call-1",
            "write_file",
            "a" * 64,
            "workspace_write",
            "reconcile_required",
            pre_state_hash="b" * 64,
        )
        assert not repeated["created"]
        with pytest.raises(RuntimeStoreError) as premature:
            await store.complete_tool(
                fence_for(current), "operation-1", "succeeded", receipt={"ok": True}
            )
        assert premature.value.code == "OPERATION_CONFLICT"

        async def physical_commit():
            calls.append("write")
            return "written"

        assert (
            await store.authorize_mutation(fence_for(current), "operation-1", physical_commit)
            == "written"
        )
        with pytest.raises(RuntimeStoreError) as replay:
            await store.authorize_mutation(fence_for(current), "operation-1", physical_commit)
        assert replay.value.code == "OPERATION_CONFLICT" and calls == ["write"]
        completed = await store.complete_tool(
            fence_for(current),
            "operation-1",
            "succeeded",
            receipt={"ok": True},
            post_state_hash="c" * 64,
        )
        duplicate = await store.complete_tool(
            fence_for(current),
            "operation-1",
            "succeeded",
            receipt={"ok": True},
            post_state_hash="c" * 64,
        )
        assert duplicate["run"]["version"] == completed["run"]["version"]
        with pytest.raises(RuntimeStoreError) as changed:
            await store.complete_tool(
                fence_for(completed["run"]),
                "operation-1",
                "succeeded",
                receipt={"ok": False},
                post_state_hash="c" * 64,
            )
        assert changed.value.code == "OPERATION_CONFLICT"

    asyncio.run(run())


def test_unresolved_physical_effect_blocks_new_run_even_after_lease_expiry(database_url, runtime):
    store, input = runtime

    async def run():
        current = await store.create(input, owner="lost-worker")
        prepared = await store.prepare_tool(
            fence_for(current),
            "operation-uncertain",
            "call-uncertain",
            "write_file",
            "a" * 64,
            "workspace_write",
            "reconcile_required",
        )
        current = prepared["run"]

        async def crash():
            raise OSError("lost physical acknowledgement")

        with pytest.raises(OSError):
            await store.authorize_mutation(fence_for(current), "operation-uncertain", crash)
        with psycopg.connect(database_url) as connection:
            connection.execute(
                "UPDATE agent_runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                (input["id"],),
            )
            connection.execute(
                "UPDATE agent_workspace_leases SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE project_id=%s",
                (input["projectId"],),
            )
        with pytest.raises(RuntimeStoreError) as blocked:
            await store.create(
                {**input, "id": "replacement-" + str(uuid4())}, owner="replacement-worker"
            )
        assert blocked.value.code == "RECONCILIATION_REQUIRED"
        candidates = await store.reconciliation_candidates(project=input["projectId"])
        assert len(candidates) == 1
        assert candidates[0]["unresolvedToolExecutions"][0]["status"] == "commit_authorized"
        taken = await store.claim(input["id"], "reconciler", current["version"])
        assert (
            taken["status"] == "reconciling" and taken["fencing_token"] > current["fencing_token"]
        )
        with pytest.raises(RuntimeStoreError) as old:
            await store.complete_tool(
                fence_for(current),
                "operation-uncertain",
                "uncertain",
                error={"code": "UNKNOWN", "message": "Ambiguous effect"},
            )
        assert old.value.code in {"CONFLICT", "LEASE_LOST"}

    asyncio.run(run())


def test_cross_project_workspace_alias_and_concurrent_run_creation_are_rejected(
    database_url, actor, runtime
):
    store, input = runtime
    other = "other-" + str(uuid4())
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Other project',%s,now())",
            (other, actor["user_id"]),
        )

    async def run():
        results = await asyncio.gather(
            store.create(input, owner="one"),
            store.create({**input, "id": "other-run-" + str(uuid4())}, owner="two"),
            return_exceptions=True,
        )
        assert sum(isinstance(item, dict) for item in results) == 1
        error = next(item for item in results if isinstance(item, Exception))
        assert isinstance(error, RuntimeStoreError) and error.code == "WORKSPACE_BUSY"
        with pytest.raises(RuntimeStoreError) as alias:
            await store.create(
                {**input, "id": "alias-" + str(uuid4()), "projectId": other, "requestId": None},
                owner="three",
            )
        assert alias.value.code == "WORKSPACE_BINDING_CONFLICT"

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM projects WHERE id=%s", (other,))


@pytest.mark.parametrize(
    "key",
    [
        "reasoning",
        "reasoning_content",
        "system-prompt",
        "messages",
        "api.key",
        "access_token",
        "constructor",
    ],
)
def test_public_runtime_projection_rejects_private_nested_fields(key):
    with pytest.raises(RuntimeStoreError):
        public_json({"nested": [{key: "private"}]})
