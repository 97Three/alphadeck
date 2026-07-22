import hashlib
import json
import os
import posixpath
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .resource_lock import sync_directory
from .skill_integrity import read_regular
from .tool_paths import no_symlinks
from .workspace import write_atomic

SCHEMA = "pi-agent-workspace-mutation-journal-v1"
DIRECTORY = ".pi-mutation-journal"


class MutationConflict(Exception):
    def __init__(self, target, message):
        self.target = target
        super().__init__(message)


def sha(content):
    return hashlib.sha256(content).hexdigest()


def inside(root, relative):
    if (
        not isinstance(relative, str)
        or not relative
        or "\\" in relative
        or "\0" in relative
        or relative.startswith("/")
        or posixpath.normpath(relative) != relative
        or relative in {".", ".."}
        or relative.startswith("../")
    ):
        raise ValueError("Unsafe mutation journal path")
    target = root / relative
    no_symlinks(root, target)
    return target


def current_hash(root, target):
    path = inside(root, target)
    if not path.exists():
        return None
    if not path.is_file():
        raise MutationConflict(target, "Mutation target is no longer a regular file")
    return sha(read_regular(path, 64 * 1024 * 1024))


def durable(path, content, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


class MutationJournal:
    def __init__(self, root, directory, manifest):
        self.root, self.directory, self.manifest = root, directory, manifest

    def set_state(self, state):
        self.manifest = {**self.manifest, "state": state}
        write_atomic(
            self.directory,
            "manifest.json",
            (json.dumps(self.manifest, ensure_ascii=False, separators=(",", ":")) + "\n").encode(),
        )
        sync_directory(self.directory)

    @classmethod
    def prepare(cls, root, run, operation, files):
        root = Path(root).resolve(strict=True)
        for identity in (run, operation):
            if (
                not isinstance(identity, str)
                or not 0 < len(identity) <= 512
                or any(char in identity for char in "\0\r\n")
            ):
                raise ValueError("Invalid mutation journal identity")
        if not 1 <= len(files) <= 8:
            raise ValueError("A mutation journal must contain between 1 and 8 files")
        journals = root / DIRECTORY
        journals.mkdir(mode=0o700, exist_ok=True)
        if journals.is_symlink() or not journals.is_dir():
            raise ValueError("Mutation journal root must be a framework-owned directory")
        os.chmod(journals, 0o700)
        sync_directory(root)
        directory = journals / sha(operation.encode())
        directory.mkdir(mode=0o700)
        try:
            (directory / "staged").mkdir(mode=0o700)
            (directory / "backups").mkdir(mode=0o700)
            manifest = {
                "schemaVersion": SCHEMA,
                "runId": run,
                "operationId": operation,
                "state": "prepared",
                "createdAt": datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "files": [],
            }
            targets = set()
            for index, file in enumerate(files):
                target = inside(root, file["target"])
                if (
                    target in targets
                    or file["existedBefore"] != (file["beforeSha256"] is not None)
                    or sha(file["content"]) != file["afterSha256"]
                ):
                    raise ValueError("Inconsistent or duplicate mutation target")
                targets.add(target)
                staged = f"staged/{index}"
                durable(directory / staged, file["content"], file["mode"] & 0o777)
                backup = None
                if file["existedBefore"]:
                    before = read_regular(target, 64 * 1024 * 1024)
                    if sha(before) != file["beforeSha256"]:
                        raise MutationConflict(
                            file["target"], "Target changed while preparing its durable backup"
                        )
                    backup = f"backups/{index}"
                    durable(directory / backup, before, file["mode"] & 0o777)
                manifest["files"].append(
                    {
                        "target": file["target"],
                        "staged": staged,
                        "backup": backup,
                        "existedBefore": file["existedBefore"],
                        "mode": file["mode"] & 0o777,
                        "beforeSha256": file["beforeSha256"],
                        "afterSha256": file["afterSha256"],
                    }
                )
            sync_directory(directory / "staged")
            sync_directory(directory / "backups")
            journal = cls(root, directory, manifest)
            journal.set_state("prepared")
            sync_directory(journals)
            return journal
        except BaseException:
            cls(root, directory, {}).cleanup()
            raise

    def commit(self):
        self.set_state("committing")
        for file in self.manifest["files"]:
            staged = inside(self.directory, file["staged"])
            target = inside(self.root, file["target"])
            if sha(read_regular(staged, 64 * 1024 * 1024)) != file["afterSha256"]:
                raise ValueError("Mutation journal staged content is corrupt")
            os.replace(staged, target)
            sync_directory(target.parent)
        self.set_state("applied")

    def rollback(self):
        if self.manifest["state"] == "rolled_back":
            return
        if self.manifest["state"] == "prepared":
            self.set_state("rolled_back")
            return
        states = []
        for file in self.manifest["files"]:
            current = current_hash(self.root, file["target"])
            if current not in {file["beforeSha256"], file["afterSha256"]}:
                raise MutationConflict(
                    file["target"], "Refusing recovery because a later edit changed the target"
                )
            if (
                file["existedBefore"]
                and sha(read_regular(inside(self.directory, file["backup"]), 64 * 1024 * 1024))
                != file["beforeSha256"]
            ):
                raise ValueError("Mutation journal backup is corrupt")
            states.append((file, current))
        for file, current in states:
            if current == file["beforeSha256"]:
                continue
            target = inside(self.root, file["target"])
            if not file["existedBefore"]:
                target.unlink(missing_ok=True)
            else:
                restore = target.parent / (
                    "." + target.name + ".pi-restore-" + str(uuid4()) + ".tmp"
                )
                backup = read_regular(inside(self.directory, file["backup"]), 64 * 1024 * 1024)
                durable(restore, backup, file["mode"])
                try:
                    os.replace(restore, target)
                finally:
                    restore.unlink(missing_ok=True)
            sync_directory(target.parent)
        self.set_state("rolled_back")

    def cleanup(self):
        expected = self.root / DIRECTORY
        if (
            self.directory.parent != expected
            or not re.fullmatch(r"[a-f0-9]{64}", self.directory.name)
            or self.directory.is_symlink()
            or self.directory.resolve().parent != expected.resolve()
            or expected.is_symlink()
        ):
            raise ValueError("Unsafe mutation journal cleanup target")
        shutil.rmtree(self.directory)
        sync_directory(expected)


def list_journals(root):
    root = Path(root).resolve(strict=True)
    journals = root / DIRECTORY
    if not journals.exists() and not journals.is_symlink():
        return []
    if journals.is_symlink() or not journals.is_dir():
        raise ValueError("Mutation journal root must be a framework-owned directory")
    result = []
    for directory in sorted(journals.iterdir()):
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or not re.fullmatch(r"[a-f0-9]{64}", directory.name)
        ):
            raise ValueError("Unexpected mutation journal entry")
        try:
            manifest = json.loads(read_regular(directory / "manifest.json", 2 * 1024 * 1024))
        except FileNotFoundError:
            MutationJournal(root, directory, {}).cleanup()
            continue
        if (
            not isinstance(manifest, dict)
            or manifest.get("schemaVersion") != SCHEMA
            or manifest.get("state") not in {"prepared", "committing", "applied", "rolled_back"}
            or not isinstance(manifest.get("files"), list)
            or not 1 <= len(manifest["files"]) <= 8
        ):
            raise ValueError("Corrupt mutation journal manifest")
        for identity in (manifest.get("runId"), manifest.get("operationId")):
            if (
                not isinstance(identity, str)
                or not 0 < len(identity) <= 512
                or any(char in identity for char in "\0\r\n")
            ):
                raise ValueError("Corrupt mutation journal identity")
        if sha(manifest["operationId"].encode()) != directory.name:
            raise ValueError("Mutation journal operation identity mismatch")
        datetime.fromisoformat(manifest["createdAt"].replace("Z", "+00:00"))
        targets = set()
        for file in manifest["files"]:
            target = inside(root, file["target"])
            staged = inside(directory, file["staged"])
            if file["backup"] is not None:
                inside(directory, file["backup"])
            if (
                target in targets
                or staged.parent != directory / "staged"
                or (
                    file["backup"] is not None
                    and (directory / file["backup"]).parent != directory / "backups"
                )
                or type(file["mode"]) is not int
                or not 0 <= file["mode"] <= 0o777
                or type(file["existedBefore"]) is not bool
                or file["existedBefore"] != (file["beforeSha256"] is not None)
                or file["existedBefore"] != (file["backup"] is not None)
                or not isinstance(file["afterSha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", file["afterSha256"])
                or (
                    file["beforeSha256"] is not None
                    and (
                        not isinstance(file["beforeSha256"], str)
                        or not re.fullmatch(r"[a-f0-9]{64}", file["beforeSha256"])
                    )
                )
            ):
                raise ValueError("Corrupt mutation journal file identity")
            targets.add(target)
        result.append(MutationJournal(root, directory, manifest))
    return result
