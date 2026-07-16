import os
import re
from pathlib import Path

from .projects import ProjectNotFound
from .resource_lock import ResourceLocked, ResourceLockLost, resource_lock
from .workspace import write_atomic

EXCLUDED_DIRECTORIES = {
    "node_modules",
    ".git",
    ".next",
    ".turbo",
    ".cache",
    ".vercel",
    ".idea",
    ".vscode",
}
SENSITIVE_DIRECTORIES = {".aws", ".azure", ".gnupg", ".secrets", ".ssh"}
SENSITIVE_FILES = {
    ".envrc",
    ".git-credentials",
    ".htpasswd",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "docker-config.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "kubeconfig",
    "secret.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "service-account.json",
    "service_account.json",
}
SENSITIVE_SUFFIXES = (
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    ".tfstate",
    ".tfstate.backup",
)


class FileBrowserError(Exception):
    def __init__(self, message, status=400):
        self.status, self.message = status, message
        super().__init__(message)


def sensitive(path):
    parts = [part.lower() for part in path.replace("\\", "/").split("/") if part not in {"", "."}]
    if not parts:
        return False
    name = parts[-1]
    return (
        bool(SENSITIVE_DIRECTORIES.intersection(parts))
        or ".." in parts
        or (name.startswith(".env") and not re.fullmatch(r"\.env\.(example|sample|template)", name))
        or name in SENSITIVE_FILES
        or name.endswith(SENSITIVE_SUFFIXES)
    )


def assert_allowed(path):
    if sensitive(path):
        raise FileBrowserError("File not found", 404)


def normalize(path):
    cleaned = re.sub(r"^\.?/", "", path.replace("\\", "/")).rstrip("/")
    return cleaned or "."


def safe_path(root: Path, relative: str):
    if not root.is_dir():
        raise FileBrowserError("Base path does not exist")
    # Keep lexical and real-path checks separate so symlinks cannot expose secrets.
    if "\x00" in relative or ":" in relative:
        raise FileBrowserError("Path traversal not allowed")
    lexical = Path(os.path.abspath(root / relative))
    if lexical != root and root not in lexical.parents:
        raise FileBrowserError("Path traversal not allowed")
    canonical = lexical.resolve()
    if canonical != root and root not in canonical.parents:
        raise FileBrowserError("Path traversal not allowed")
    assert_allowed(canonical.relative_to(root).as_posix())
    return lexical


def visible(entry, relative):
    return (
        not entry.is_symlink()
        and not sensitive(relative)
        and not (entry.is_dir(follow_symlinks=False) and entry.name in EXCLUDED_DIRECTORIES)
        and entry.name != ".DS_Store"
    )


class FileBrowser:
    def __init__(self, projects, *, lock_wait_seconds=5):
        self.projects, self.lock_wait_seconds = projects, lock_wait_seconds

    async def root(self, project_id):
        try:
            project = await self.projects.get(project_id)
            return self.projects.managed_path(project, must_exist=True)
        except ProjectNotFound:
            raise FileBrowserError("Project not found", 404) from None
        except ValueError as error:
            raise FileBrowserError(str(error)) from error

    async def list(self, project_id, directory="."):
        root = await self.root(project_id)
        relative = normalize(directory)
        assert_allowed(relative)
        target = safe_path(root, relative)
        if not target.exists():
            raise FileBrowserError("Directory not found", 404)
        if not target.is_dir():
            raise FileBrowserError("Not a directory")
        result = []
        with os.scandir(target) as entries:
            for entry in entries:
                child = entry.name if relative == "." else f"{relative}/{entry.name}"
                if not visible(entry, child):
                    continue
                actual = safe_path(root, child)
                if entry.is_dir(follow_symlinks=False):
                    with os.scandir(actual) as children:
                        has_children = any(
                            visible(item, f"{child}/{item.name}") for item in children
                        )
                    result.append(
                        {
                            "name": entry.name,
                            "path": child,
                            "type": "directory",
                            "hasChildren": has_children,
                        }
                    )
                else:
                    result.append(
                        {
                            "name": entry.name,
                            "path": child,
                            "type": "file",
                            "size": actual.stat().st_size,
                            "hasChildren": False,
                        }
                    )
        return sorted(
            result,
            key=lambda item: (item["type"] != "directory", item["name"].casefold(), item["name"]),
        )

    async def read(self, project_id, path):
        root = await self.root(project_id)
        relative = normalize(path)
        assert_allowed(relative)
        target = safe_path(root, relative)
        try:
            metadata = target.stat()
        except FileNotFoundError:
            raise FileBrowserError("File not found", 404) from None
        if not target.is_file():
            raise FileBrowserError("Not a file")
        if metadata.st_size > 500000:
            raise FileBrowserError("File too large to display")
        with target.open("rb") as source:
            content = source.read(500001)
        if len(content) > 500000:
            raise FileBrowserError("File too large to display")
        return {"path": relative, "content": content.decode("utf-8", errors="replace")}

    async def write(self, project_id, path, content):
        root = await self.root(project_id)
        if not isinstance(content, str):
            raise FileBrowserError("Invalid file content")
        if len(content.encode("utf-16-le", errors="surrogatepass")) // 2 > 1000000:
            raise FileBrowserError("File content too large")
        try:
            async with resource_lock(
                root,
                {"purpose": "other", "projectId": project_id},
                wait_seconds=self.lock_wait_seconds,
            ):
                relative = normalize(path)
                assert_allowed(relative)
                target = safe_path(root, relative)
                if not target.exists():
                    raise FileBrowserError("File not found", 404)
                if not target.is_file():
                    raise FileBrowserError("Not a file")
                canonical = target.resolve()
                write_atomic(root, canonical.relative_to(root).as_posix(), content.encode("utf-8"))
        except ResourceLocked:
            raise FileBrowserError(
                "Project files are busy; retry after generation finishes", 409
            ) from None
        except ResourceLockLost:
            raise FileBrowserError("Failed to write file", 500) from None
