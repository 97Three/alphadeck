import asyncio
import io
import json
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest

from alphadeck_platform.auth_cleanup import cleanup
from alphadeck_platform.config import Settings
from alphadeck_platform.database import Database
from alphadeck_platform.release_backup import (
    backup,
    database_environment,
    inspect_archive,
    restore,
)
from alphadeck_platform.state_migration import migrate_platform_state, sync_workspaces


def test_native_workspace_recovery_and_state_import_are_idempotent(database_url, tmp_path):
    database = Database(database_url)
    project = "project-" + uuid4().hex
    root = tmp_path / "projects"
    workspace = root / project
    (workspace / ".data-agent").mkdir(parents=True)
    (workspace / ".data-agent/workspace.json").write_text(
        json.dumps({"projectName": "Recovered", "runtime": {"modelId": "deepseek-v4-flash"}})
    )
    queue_id = "legacy-" + uuid4().hex
    (tmp_path / "tmp/alphadeck-eval-queue").mkdir(parents=True)
    (tmp_path / "tmp/alphadeck-eval-queue/queue.json").write_text(
        json.dumps([{"id": queue_id, "status": "running", "pid": 42}])
    )

    async def run():
        try:
            assert await sync_workspaces(database, root) == {"created": 1, "skipped": 0}
            assert await sync_workspaces(database, root) == {"created": 0, "skipped": 1}
            assert (await migrate_platform_state(database, tmp_path))["evalQueue"] == 1
            assert (await migrate_platform_state(database, tmp_path))["evalQueue"] == 0
            row = await database.fetch_one(
                "SELECT status,pid,lease_token FROM eval_queue_items WHERE id=%s", (queue_id,)
            )
            assert row == {"status": "failed", "pid": None, "lease_token": None}
        finally:
            async with database.transaction() as tx:
                await tx.execute("DELETE FROM projects WHERE id=%s", (project,))
                await tx.execute("DELETE FROM eval_queue_items WHERE id=%s", (queue_id,))

    asyncio.run(run())


def test_native_auth_cleanup_dry_run_never_mutates_sessions(database_url, actor):
    database = Database(database_url)
    identity = str(uuid4())

    async def run():
        async with database.transaction() as tx:
            await tx.execute(
                "INSERT INTO auth_sessions (id,token,user_id,expires_at,updated_at) VALUES (%s,%s,%s,now()-interval '2 years',now())",
                (identity, str(uuid4()), actor["user_id"]),
            )
        simulated = await cleanup(database, dry_run=True)
        assert simulated["sessions"] >= 1
        assert await database.fetch_one("SELECT id FROM auth_sessions WHERE id=%s", (identity,))
        actual = await cleanup(database)
        assert actual["sessions"] >= 1
        assert not await database.fetch_one("SELECT id FROM auth_sessions WHERE id=%s", (identity,))
        assert await database.fetch_one(
            "SELECT id FROM auth_sessions WHERE id=%s", (actor["session_id"],)
        )

    asyncio.run(run())


def test_backup_manifest_restore_confirmation_and_snapshot_retention(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "source.txt").write_text("original")
    settings = Settings(
        "postgresql://user:password@localhost:35435/migration_test?schema=public",
        projects,
        "http://localhost",
    )
    monkeypatch.setattr("alphadeck_platform.release_backup.ROOT", tmp_path)
    calls = []

    def controlled_process(command, **kwargs):
        calls.append(command)
        assert "password" not in " ".join(command)
        if command[0] == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"controlled-database-dump")

    destination = tmp_path / "backup"
    assert backup(settings, destination, run=controlled_process) == str(destination)
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["database"] == "migration_test"
    assert "password" not in json.dumps(manifest)
    count = len(calls)
    with pytest.raises(ValueError, match="confirm-database"):
        restore(
            settings,
            destination,
            confirm_database="other",
            replace_files=True,
            run=controlled_process,
        )
    assert len(calls) == count
    (projects / "source.txt").write_text("newer")
    retained = restore(
        settings,
        destination,
        confirm_database="migration_test",
        replace_files=True,
        run=controlled_process,
    )
    assert (projects / "source.txt").read_text() == "original"
    assert (Path(retained[0]) / "source.txt").read_text() == "newer"
    (destination / "database.dump").write_text("tampered")
    count = len(calls)
    with pytest.raises(ValueError, match="checksum"):
        restore(
            settings,
            destination,
            confirm_database="migration_test",
            replace_files=True,
            run=controlled_process,
        )
    assert len(calls) == count
    database, environment = database_environment(settings.database_url)
    assert database == "migration_test" and environment["PGPASSWORD"] == "password"


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("fifo", tarfile.FIFOTYPE),
    ],
)
def test_restore_rejects_unsafe_archive_before_any_database_command(tmp_path, name, kind):
    path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.size = 1 if kind == tarfile.REGTYPE else 0
        member.linkname = "/outside" if kind == tarfile.SYMTYPE else ""
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe"):
        inspect_archive(path)
