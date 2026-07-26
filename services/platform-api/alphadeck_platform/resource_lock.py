"""Physical write lock compatible with the original Node workspace writer."""

import asyncio
import json
import os
import shutil
import socket
import stat
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .skill_integrity import read_regular

LOCK_DIRECTORY = ".pi-workspace.lock"


class ResourceLocked(Exception):
    pass


class ResourceLockLost(Exception):
    pass


def owner(lock: Path):
    try:
        value = json.loads((lock / "owner.json").read_text())
        return value.get("ownerId") if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def observation(lock):
    try:
        value = json.loads(read_regular(lock / "owner.json", 4096))
        if (
            value.get("schemaVersion") != 2
            or not isinstance(value.get("ownerId"), str)
            or not value["ownerId"]
            or type(value.get("pid")) is not int
            or value["pid"] <= 0
            or not isinstance(value.get("hostname"), str)
            or not value["hostname"]
        ):
            return None
        metadata = (lock / "owner.json").stat()
        return {
            "owner": {key: value[key] for key in ("schemaVersion", "ownerId", "pid", "hostname")},
            "device": str(metadata.st_dev),
            "inode": str(metadata.st_ino),
        }
    except (ValueError, OSError, KeyError, TypeError):
        return None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def sync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover_dead_owner(lock, root):
    observed = observation(lock)
    if (
        not observed
        or observed["owner"]["hostname"] != socket.gethostname()
        or alive(observed["owner"]["pid"])
    ):
        return False
    claim_id = str(uuid4())
    claim = {
        "schemaVersion": 1,
        "claimId": claim_id,
        "claimantPid": os.getpid(),
        "claimantHostname": socket.gethostname(),
        "targetOwner": observed["owner"],
        "targetDevice": observed["device"],
        "targetInode": observed["inode"],
    }
    claim_path = lock / ".recovery-claim.json"
    try:
        with claim_path.open("x", encoding="utf-8") as output:
            json.dump(claim, output)
    except (FileExistsError, FileNotFoundError):
        return False
    quarantine = root / (LOCK_DIRECTORY + ".dead-" + str(uuid4()))
    moved = False
    try:
        if observation(lock) != observed or alive(observed["owner"]["pid"]):
            return False
        lock.rename(quarantine)
        moved = True
    except FileNotFoundError:
        return False
    finally:
        if not moved:
            try:
                if json.loads(read_regular(claim_path, 4096)).get("claimId") == claim_id:
                    claim_path.unlink()
            except (OSError, ValueError):
                pass
    sync_directory(root)
    try:
        quarantined_claim = json.loads(read_regular(quarantine / ".recovery-claim.json", 4096))
    except (OSError, ValueError) as error:
        raise ResourceLockLost(
            "Quarantined lock identity requires manual reconciliation"
        ) from error
    if (
        observation(quarantine) != observed
        or quarantined_claim != claim
        or quarantine.is_symlink()
        or quarantine.resolve().parent != root
    ):
        raise ResourceLockLost("Quarantined lock identity changed")
    shutil.rmtree(quarantine)
    sync_directory(root)
    return True


@asynccontextmanager
async def resource_lock(
    root: Path, metadata=None, *, wait_seconds=5, recover_dead_local_owner=False
):
    if not 0 < wait_seconds <= 3600:
        raise ValueError("Invalid resource lock wait time")
    allowed_metadata = {
        "purpose",
        "projectId",
        "requestId",
        "runId",
        "operationId",
        "missionId",
        "generationId",
    }
    if metadata and metadata.keys() - allowed_metadata:
        raise ValueError("Unknown workspace lock metadata")
    if metadata and any(
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode()) > 512
        or any(character in value for character in "\r\n\0")
        for value in metadata.values()
    ):
        raise ValueError("Invalid workspace lock metadata value")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Resource lock root must be a directory")
    lock = root / LOCK_DIRECTORY
    identity = f"resource:{os.getpid()}:{uuid4()}"
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if recover_dead_local_owner and recover_dead_owner(lock, root):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResourceLocked(
                    "Workspace physical mutation lock is held or requires reconciliation"
                ) from None
            await asyncio.sleep(min(0.05, remaining))
    acquired = lock.stat()
    written = False
    created = False
    try:
        # The filename and schema are shared with the TS runtime during migration.
        with (lock / "owner.json").open("x", encoding="utf-8") as output:
            created = True
            json.dump(
                {
                    "schemaVersion": 2,
                    "ownerId": identity,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "instanceId": os.getenv(
                        "PI_AGENT_INSTANCE_ID", f"{socket.gethostname()}:{os.getpid()}"
                    ),
                    "acquiredAt": datetime.now(UTC).isoformat(),
                    **(metadata or {}),
                },
                output,
            )
            output.flush()
            os.fsync(output.fileno())
        written = True
        yield
    finally:
        current = lock.lstat()
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (acquired.st_dev, acquired.st_ino)
            or (written and owner(lock) != identity)
        ):
            raise ResourceLockLost("Workspace resource lock ownership changed")
        # Never recursively remove an ambiguous or replacement lock directory.
        if created:
            (lock / "owner.json").unlink()
        lock.rmdir()
