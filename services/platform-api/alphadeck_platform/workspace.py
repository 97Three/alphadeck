import os
import re
import stat
import tempfile
from pathlib import Path

PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")


def project_path(root: Path, project_id: str) -> Path:
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("Invalid project ID")
    root = root.resolve()
    candidate = root / project_id
    result = candidate.resolve()
    if candidate.is_symlink() or result != candidate:
        raise ValueError("Workspace resolves outside the projects directory")
    return result


def workspace_file(root: Path, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    if not normalized or "\x00" in normalized or ":" in normalized:
        raise ValueError("Invalid workspace path")
    if normalized.startswith("/") or ".." in normalized.split("/"):
        raise ValueError("Workspace path must be relative")
    root = root.resolve()
    result = (root / normalized).resolve()
    if result == root or root not in result.parents:
        raise ValueError("Workspace path escapes its root")
    return result


def write_atomic(root: Path, relative: str, content: bytes) -> Path:
    target = workspace_file(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".alphadeck-", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            if target.is_file():
                os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target
