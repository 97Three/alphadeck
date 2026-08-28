import asyncio

import pytest
from test_run_engine import engine_context  # noqa: F401

from alphadeck_platform.mutation_journal import MutationJournal, list_journals, sha
from alphadeck_platform.provenance import provenance_hash
from alphadeck_platform.recovery import assert_checkpoint, reconcile_expired
from alphadeck_platform.runtime_store import RuntimeStore, fence_for


@pytest.mark.parametrize(
    "scenario",
    ["prepared", "applied", "later-edit", "external", "uncertain", "missing-journal", "live-owner"],
)
def test_recovery_rolls_back_only_known_expired_workspace_writes(engine_context, scenario):  # noqa: F811
    database, input, _, root = engine_context
    (root / "app").mkdir()
    target = root / "app/page.tsx"
    target.write_bytes(b"before")

    async def run():
        store = RuntimeStore(database)
        row = await store.create(input, owner="crashed-worker")
        prepared = await store.prepare_tool(
            fence_for(row),
            "interrupted-operation",
            "call",
            "write_file",
            "a" * 64,
            "external_write" if scenario == "external" else "workspace_write",
            "reconcile_required",
        )
        row = prepared["run"]
        journal = None
        if scenario in {"prepared", "applied", "later-edit", "live-owner"}:
            journal = MutationJournal.prepare(
                root,
                input["id"],
                "interrupted-operation",
                [
                    {
                        "target": "app/page.tsx",
                        "content": b"after",
                        "existedBefore": True,
                        "mode": 0o644,
                        "beforeSha256": sha(b"before"),
                        "afterSha256": sha(b"after"),
                    }
                ],
            )
        if scenario in {"applied", "later-edit", "missing-journal", "live-owner"}:

            async def commit():
                if journal:
                    journal.commit()

            await store.authorize_mutation(fence_for(row), "interrupted-operation", commit)
        if scenario == "uncertain":
            result = await store.complete_tool(
                fence_for(row),
                "interrupted-operation",
                "uncertain",
                error={"code": "UNCERTAIN", "message": "Unknown outcome"},
            )
            row = result["run"]
        if scenario == "later-edit":
            target.write_bytes(b"user edit")
        if scenario != "live-owner":
            async with database.transaction() as connection:
                await connection.execute(
                    "UPDATE agent_runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                    (input["id"],),
                )
                await connection.execute(
                    "UPDATE agent_workspace_leases SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE project_id=%s",
                    (input["projectId"],),
                )
        result = await reconcile_expired(database, input["projectId"], root)
        if scenario in {"prepared", "applied"}:
            assert result["interruptedRunIds"] == [input["id"]]
            assert target.read_bytes() == b"before"
            assert list_journals(root) == []
            assert (await store.get(input["id"]))["status"] == "interrupted"
        elif scenario == "live-owner":
            assert result["racedRunIds"] == [input["id"]]
            assert target.read_bytes() == b"after"
        else:
            assert result["blocked"][0]["runId"] == input["id"]
            assert not result["interruptedRunIds"]
            if scenario == "later-edit":
                assert target.read_bytes() == b"user edit"

    asyncio.run(run())


def test_corrupt_checkpoint_is_rejected_before_recovery_side_effects():
    checkpoint = {
        "state_version": 2,
        "state_hash": "sha256:" + "0" * 64,
        "public_state": {"stage": "run_started"},
        "boundary": "run_started",
    }
    with pytest.raises(ValueError, match="hash check"):
        assert_checkpoint(checkpoint)
    checkpoint["state_hash"] = "sha256:" + provenance_hash(checkpoint["public_state"])
    assert_checkpoint(checkpoint)
    checkpoint["state_version"] = 3
    with pytest.raises(ValueError, match="Unsupported"):
        assert_checkpoint(checkpoint)
