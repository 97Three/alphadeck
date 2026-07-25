import hashlib
import json
import math
import os
import stat
from pathlib import Path

import rfc8785


def object_keys(value):
    numeric = []
    other = []
    for key in value:
        if (
            len(key) <= 10
            and key.isascii()
            and key.isdecimal()
            and str(int(key)) == key
            and int(key) < 4294967295
        ):
            numeric.append(key)
        else:
            other.append(key)
    return sorted(numeric, key=int) + other


def js_json(value, *, indent=None, sort_keys=False):
    ancestors = set()

    def encode(item, level):
        if isinstance(item, str):
            # JS strings use UTF-16: concatenated slices can join two surrogate halves.
            item = item.encode("utf-16-le", errors="surrogatepass").decode(
                "utf-16-le", errors="surrogatepass"
            )
            text = json.dumps(item, ensure_ascii=False)
            return "".join(
                f"\\u{ord(char):04x}" if 0xD800 <= ord(char) <= 0xDFFF else char for char in text
            )
        if type(item) is int and abs(item) > 9007199254740991:
            item = float(item)
        if item is None or type(item) in {bool, int, float}:
            if type(item) is float and not math.isfinite(item):
                raise ValueError("Cannot serialize non-finite numbers")
            return rfc8785.dumps(item).decode()
        if type(item) not in {dict, list} or id(item) in ancestors:
            raise ValueError("Cannot serialize cyclic or non-JSON values")
        ancestors.add(id(item))
        try:
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("JSON object keys must be strings")
                keys = (
                    sorted(item, key=lambda key: key.encode("utf-16-be", errors="surrogatepass"))
                    if sort_keys
                    else object_keys(item)
                )
                values = [
                    encode(key, level + 1)
                    + (": " if indent is not None else ":")
                    + encode(item[key], level + 1)
                    for key in keys
                ]
                start, end = "{", "}"
            else:
                values = [encode(child, level + 1) for child in item]
                start, end = "[", "]"
            if not values:
                return start + end
            if indent is None:
                return start + ",".join(values) + end
            padding = " " * (indent * (level + 1))
            return (
                start
                + "\n"
                + padding
                + (",\n" + padding).join(values)
                + "\n"
                + " " * (indent * level)
                + end
            )
        finally:
            ancestors.remove(id(item))

    return encode(value, 0)


def canonical_json(value):
    return js_json(value, sort_keys=True).encode()


def provenance_hash(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def frame(digest, label, value):
    data = value.encode() if isinstance(value, str) else value
    digest.update(label.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)


def workspace_identity(root, namespace=None):
    namespace = (
        namespace
        if namespace is not None
        else os.getenv("PI_AGENT_WORKSPACE_NAMESPACE", "alphadeck-local")
    )
    if not namespace.strip() or len(namespace.encode()) > 256:
        raise ValueError("Workspace namespace must be between 1 and 256 UTF-8 bytes")
    digest = hashlib.sha256()
    for label, value in (
        ("version", "pi-agent-workspace-identity-v1"),
        ("namespace", namespace),
        ("canonical-root", str(Path(root).resolve(strict=True))),
    ):
        frame(digest, label, value)
    return "sha256:" + digest.hexdigest()


def workspace_snapshot(
    root, *, max_files=20000, max_hashed_bytes=67108864, max_file_hash_bytes=8388608
):
    for value in (max_files, max_hashed_bytes, max_file_hash_bytes):
        if type(value) is not int or value <= 0:
            raise ValueError("Workspace snapshot budgets must be positive integers")
    root = Path(root).resolve(strict=True)
    digest = hashlib.sha256()
    frame(digest, "version", "pi-agent-workspace-snapshot-v1")
    count, hashed, metadata = 0, 0, 0

    def visit(directory, depth=0):
        nonlocal count, hashed, metadata
        if depth > 256:
            raise ValueError("Workspace snapshot exceeded maximum directory depth")
        for entry in sorted(
            directory.iterdir(),
            key=lambda path: path.name.encode("utf-16-be", errors="surrogatepass"),
        ):
            info = entry.lstat()
            relative = entry.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode) and entry.name in {
                ".git",
                ".next",
                "node_modules",
                ".pi-mutation-journal",
                ".pi-workspace.lock",
            }:
                continue
            if stat.S_ISDIR(info.st_mode):
                frame(digest, "directory", relative)
                visit(entry, depth + 1)
                continue
            if not stat.S_ISLNK(info.st_mode) and not stat.S_ISREG(info.st_mode):
                continue
            count += 1
            if count > max_files:
                raise ValueError("Workspace snapshot exceeded maximum entries")
            if stat.S_ISLNK(info.st_mode):
                frame(digest, "symlink-path", relative)
                frame(digest, "symlink-target", os.readlink(entry))
                continue
            frame(digest, "file-path", relative)
            frame(digest, "file-size", str(info.st_size))
            if info.st_size <= max_file_hash_bytes and hashed + info.st_size <= max_hashed_bytes:
                descriptor = os.open(
                    entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                )
                with os.fdopen(descriptor, "rb") as stream:
                    current = os.fstat(stream.fileno())
                    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
                        info.st_dev,
                        info.st_ino,
                    ):
                        raise ValueError("Workspace changed while taking its snapshot")
                    data = stream.read(max_file_hash_bytes + 1)
                    final = os.fstat(stream.fileno())
                if len(data) != info.st_size or (final.st_size, final.st_mtime_ns) != (
                    info.st_size,
                    info.st_mtime_ns,
                ):
                    raise ValueError("Workspace changed while taking its snapshot")
                hashed += len(data)
                frame(digest, "file-content", data)
            else:
                metadata += 1
                frame(digest, "file-metadata-only", f"{info.st_size}:{info.st_mtime_ns // 1000000}")

    visit(root)
    return {
        "sha256": digest.hexdigest(),
        "fileCount": count,
        "hashedBytes": hashed,
        "metadataOnlyFiles": metadata,
    }
