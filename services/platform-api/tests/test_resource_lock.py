import asyncio
import json
import socket

import pytest

from alphadeck_platform.resource_lock import ResourceLocked, ResourceLockLost, resource_lock


def test_ambiguous_existing_lock_is_preserved(tmp_path):
    lock = tmp_path / ".pi-workspace.lock"
    lock.mkdir()
    (lock / "owner.json").write_text("corrupt metadata")

    async def run():
        with pytest.raises(ResourceLocked):
            async with resource_lock(tmp_path, wait_seconds=0.02):
                pytest.fail("An ambiguous existing lock must not be acquired")

    asyncio.run(run())
    assert (lock / "owner.json").read_text() == "corrupt metadata"


def test_replaced_owner_is_not_removed_during_release(tmp_path):
    async def run():
        with pytest.raises(ResourceLockLost):
            async with resource_lock(tmp_path):
                owner = tmp_path / ".pi-workspace.lock/owner.json"
                metadata = json.loads(owner.read_text())
                metadata["ownerId"] = "replacement-owner"
                owner.write_text(json.dumps(metadata))

    asyncio.run(run())
    metadata = json.loads((tmp_path / ".pi-workspace.lock/owner.json").read_text())
    assert metadata["ownerId"] == "replacement-owner"


def test_explicit_startup_recovery_only_reclaims_provably_dead_local_owner(tmp_path):
    lock = tmp_path / ".pi-workspace.lock"
    lock.mkdir()
    owner = {
        "schemaVersion": 2,
        "ownerId": "old",
        "pid": 999999999,
        "hostname": socket.gethostname(),
    }
    (lock / "owner.json").write_text(json.dumps(owner))

    async def run():
        async with resource_lock(tmp_path, recover_dead_local_owner=True):
            assert json.loads((lock / "owner.json").read_text())["ownerId"] != "old"

    asyncio.run(run())
    assert not lock.exists()
    assert not list(tmp_path.glob(".pi-workspace.lock.dead-*"))
