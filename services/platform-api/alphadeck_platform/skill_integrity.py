"""Verify the source catalogue before project initialization consumes it."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .config import ROOT


def read_regular(path: Path, maximum: int):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if path.is_symlink():
        raise ValueError("Skill file must not be a symlink")
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("Skill file is not regular or exceeds its byte limit")
        content = source.read(maximum + 1)
        if len(content) > maximum:
            raise ValueError("Skill file exceeds its byte limit")
        return content


def hash_directory(directory: Path):
    files = []
    entries = 0

    def visit(current, depth):
        nonlocal entries
        if current.is_symlink() or not current.is_dir() or depth > 16:
            raise ValueError("Unsafe Skill directory")
        with os.scandir(current) as children:
            for child in children:
                if child.name == ".DS_Store":
                    continue
                entries += 1
                if entries > 250:
                    raise ValueError("Skill tree exceeds its entry limit")
                path = Path(child.path)
                if child.is_dir(follow_symlinks=False):
                    visit(path, depth + 1)
                elif child.is_file(follow_symlinks=False):
                    files.append(path)
                else:
                    raise ValueError("Skill tree contains a link or special file")

    visit(directory, 0)
    digest = hashlib.sha256()
    total = 0
    for path in sorted(files, key=lambda item: item.as_posix()):
        content = read_regular(path, 4 * 1024 * 1024)
        total += len(content)
        if total > 50 * 1024 * 1024:
            raise ValueError("Skill tree exceeds its byte limit")
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return {"hash": digest.hexdigest(), "fileCount": len(files)}


def verify_source_catalog(root=ROOT):
    registry = json.loads(read_regular(root / ".pi/skills.registry.json", 1024 * 1024))
    lock = json.loads(read_regular(root / ".pi/skills.lock.json", 1024 * 1024))
    if registry.get("schemaVersion") != 1 or lock.get("schemaVersion") != 1:
        raise ValueError("Unsupported skill catalogue schema")
    results = {}
    for skill in registry["coreSkills"]:
        identity = skill["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identity) or identity in results:
            raise ValueError("Invalid or duplicate Skill ID")
        expected = lock["skills"][identity]
        actual = hash_directory(root / ".pi/skills" / identity)
        if (
            expected["version"] != skill["version"]
            or expected["sourceSha256"] != actual["hash"]
            or expected["fileCount"] != actual["fileCount"]
        ):
            raise ValueError(f"Skill source hash or version mismatch: {identity}")
        results[identity] = actual
    return results
