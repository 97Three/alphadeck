"""Verified native backup/restore CLI. Restore requires explicit target confirmation."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from .build_identity import build_identity
from .config import ROOT, Settings
from .skill_integrity import read_regular
from .skill_source import strict_json

ALLOWED = {"database.dump", "workspaces.tar.gz", "uploads.tar.gz"}


def database_environment(url):
    parsed = urlsplit(url)
    database = unquote(parsed.path.removeprefix("/"))
    if parsed.scheme not in ("postgresql", "postgres") or not parsed.hostname or not database:
        raise ValueError("DATABASE_URL must identify a PostgreSQL database")
    environment = {
        **os.environ,
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGUSER": unquote(parsed.username or ""),
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": database,
    }
    if mode := parse_qs(parsed.query).get("sslmode"):
        environment["PGSSLMODE"] = mode[0]
    return database, environment


def fingerprint():
    value = os.getenv("ENCRYPTION_KEY", "")
    return hashlib.sha256(value.encode()).hexdigest()[:16] if value else None


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1048576):
            value.update(chunk)
    return value.hexdigest()


def archive(source, target):
    source = Path(source)
    if not source.is_dir():
        return False
    if source.is_symlink():
        raise ValueError("Backup source must be a physical directory")

    def include(member):
        # Rebuildable caches and shared dependency links are not user source state.
        if set(Path(member.name).parts) & {
            "node_modules",
            ".next",
            ".git",
            ".ruff_cache",
            "__pycache__",
        }:
            return None
        if member.issym() or member.islnk():
            raise ValueError("Backup refuses symbolic or hard links in user state")
        return member

    with tarfile.open(target, "w:gz", dereference=False) as output:
        output.add(source, arcname=".", filter=include)
    return True


def inspect_archive(path):
    with tarfile.open(path, "r:gz") as archive_file:
        total = 0
        for count, member in enumerate(archive_file, 1):
            total += member.size
            if count > 1000000 or total > 100 * 1024**3:
                raise ValueError("Backup archive exceeds its entry/size limit")
            if (
                member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or member.name.startswith(("/", "\\"))
                or ".." in member.name.replace("\\", "/").split("/")
            ):
                raise ValueError("Unsafe backup archive member")


def backup(settings, output, *, run=subprocess.run):
    output = Path(output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise ValueError("Backup destination already exists")
    staging = output.with_name(output.name + ".partial-" + uuid4().hex)
    staging.mkdir(mode=0o700)
    database, environment = database_environment(settings.database_url)
    try:
        run(
            ["pg_dump", "--format=custom", "--no-owner", "--file", str(staging / "database.dump")],
            env=environment,
            check=True,
        )
        files = [{"kind": "database", "name": "database.dump"}]
        for kind, source in (
            ("workspaces", settings.projects_dir),
            ("uploads", ROOT / "public/uploads"),
        ):
            if archive(source, staging / (kind + ".tar.gz")):
                files.append({"kind": kind, "name": kind + ".tar.gz"})
        for item in files:
            path = staging / item["name"]
            item.update(bytes=path.stat().st_size, sha256=digest(path))
        manifest = {
            "schemaVersion": 1,
            "createdAt": datetime.now(UTC).isoformat(),
            "database": database,
            "gitRevision": build_identity()["gitRevision"],
            "encryptionKeyFingerprint": fingerprint(),
            "files": files,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
        for path in staging.iterdir():
            path.chmod(0o600)
        staging.rename(output)
        return str(output)
    finally:
        if staging.exists():
            # The staging target was created under this exact output parent by this call.
            if staging.parent != output.parent or not staging.name.startswith(
                output.name + ".partial-"
            ):
                raise ValueError("Unexpected backup staging path")
            shutil.rmtree(staging)


def restore(settings, source, *, confirm_database, replace_files, run=subprocess.run):
    source = Path(source).resolve(strict=True)
    manifest = strict_json(read_regular(source / "manifest.json", 1048576))
    database, environment = database_environment(settings.database_url)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 1
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("Invalid backup manifest")
    names = [item.get("name") for item in manifest["files"] if isinstance(item, dict)]
    if (
        len(names) != len(manifest["files"])
        or any(not isinstance(name, str) or name not in ALLOWED for name in names)
        or len(set(names)) != len(names)
        or "database.dump" not in names
    ):
        raise ValueError("Backup contains missing, repeated or unexpected files")
    if database != confirm_database or manifest.get("database") != database or not replace_files:
        raise ValueError(
            "Restore requires matching --confirm-database and explicit --replace-files"
        )
    if (
        manifest.get("encryptionKeyFingerprint")
        and manifest["encryptionKeyFingerprint"] != fingerprint()
    ):
        raise ValueError("ENCRYPTION_KEY does not match the backup fingerprint")
    destinations = (
        ("workspaces.tar.gz", settings.projects_dir),
        ("uploads.tar.gz", ROOT / "public/uploads"),
    )
    for name, destination in destinations:
        if name in names:
            destination = Path(destination).absolute()
            if (
                destination.is_symlink()
                or destination.resolve() != destination
                or destination == Path(destination.anchor)
            ):
                raise ValueError("Restore destination must be an explicit physical data directory")
    for item in manifest["files"]:
        path = source / item["name"]
        if (
            path.is_symlink()
            or path.resolve().parent != source
            or digest(path) != item.get("sha256")
        ):
            raise ValueError("Backup checksum mismatch: " + item["name"])
        if item["name"].endswith(".tar.gz"):
            inspect_archive(path)
    run(
        ["pg_restore", "--list", str(source / "database.dump")],
        env=environment,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    run(
        [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--exit-on-error",
            "--dbname",
            database,
            str(source / "database.dump"),
        ],
        env=environment,
        check=True,
    )
    retained = []
    for name, destination in destinations:
        if name not in names:
            continue
        destination = Path(destination).absolute()
        if (
            destination.is_symlink()
            or destination.resolve() != destination
            or destination == Path(destination.anchor)
        ):
            raise ValueError("Restore destination must be an explicit physical data directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        identity = uuid4().hex
        staging = destination.with_name(destination.name + ".restore-" + identity)
        previous = destination.with_name(destination.name + ".pre-restore-" + identity)
        staging.mkdir(mode=0o700)
        with tarfile.open(source / name, "r:gz") as archive_file:
            archive_file.extractall(staging, filter="data")
        if destination.exists():
            destination.rename(previous)
            retained.append(str(previous))
        try:
            staging.rename(destination)
        except OSError:
            if previous.exists() and not destination.exists():
                previous.rename(destination)
            raise
    return retained


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("backup", "restore"))
    parser.add_argument("--output")
    parser.add_argument("--backup")
    parser.add_argument("--confirm-database")
    parser.add_argument("--replace-files", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "backup":
        destination = args.output or str(
            Path(os.getenv("ALPHADECK_BACKUP_ROOT", str(ROOT / "backups")))
            / ("alphadeck-" + datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ"))
        )
        print("Verified backup: " + backup(settings, destination))
    else:
        if not args.backup:
            parser.error("--backup is required")
        print(
            json.dumps(
                {
                    "retainedSnapshots": restore(
                        settings,
                        args.backup,
                        confirm_database=args.confirm_database,
                        replace_files=args.replace_files,
                    )
                }
            )
        )


if __name__ == "__main__":
    main()
