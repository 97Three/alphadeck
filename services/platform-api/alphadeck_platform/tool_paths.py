import ntpath
import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

VIRTUAL = {
    ".data-agent",
    "app",
    "components",
    "data_file",
    "evidence",
    "hooks",
    "lib",
    "public",
    "src",
    "styles",
    "types",
    "uploads",
}
SENSITIVE_DIRS = {
    ".aws",
    ".azure",
    ".git",
    ".gnupg",
    ".pi-mutation-journal",
    ".pi-workspace.lock",
    ".ssh",
}
WRITE_DIRS = {
    ".git",
    ".pi-mutation-journal",
    ".pi-workspace.lock",
    ".next",
    ".data-agent",
    "node_modules",
    "scripts",
}
SENSITIVE_FILES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".yarnrc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
LOCK_FILES = {
    "bun.lock",
    "bun.lockb",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}
CONFIG = re.compile(
    r"(?:next|postcss|tailwind|eslint|vitest|jest|playwright)\.config\.[cm]?[jt]s", re.I
)


class ToolError(Exception):
    def __init__(self, code, message, details=None):
        self.code, self.message, self.details = code, message, details
        super().__init__(message)


def matches_glob(relative, glob):
    value = relative.replace("\\", "/").removeprefix("./")
    pattern = glob.replace("\\", "/").removeprefix("./")
    index = 0
    expression = ""
    while index < len(pattern):
        if pattern[index : index + 3] == "**/":
            expression += "(?:.*/)?"
            index += 3
        elif pattern[index : index + 2] == "**":
            expression += ".*"
            index += 2
        elif pattern[index] == "*":
            expression += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            expression += "[^/]"
            index += 1
        else:
            expression += re.escape(pattern[index])
            index += 1
    return re.fullmatch(expression, value) is not None


def normalize(value, allow_root=False):
    if not isinstance(value, str) or not value or any(char in value for char in "\0\r\n"):
        raise ToolError("INVALID_PATH", "A non-empty workspace-relative path is required.")
    first = re.match(r"^/([^/]+)(?:/|$)", value)
    if first and first[1] in VIRTUAL:
        value = value[1:]
    if value.startswith("/") or ntpath.isabs(value):
        raise ToolError("ABSOLUTE_PATH_DENIED", "Tools accept workspace-relative paths only.")
    if "\\" in value:
        raise ToolError("INVALID_PATH", "Use forward slashes in workspace paths.")
    value = posixpath.normpath(value.removeprefix("./"))
    if value == ".." or value.startswith("../"):
        raise ToolError("PATH_TRAVERSAL_DENIED", "Path traversal outside the workspace is denied.")
    if not allow_root and value in {".", ""}:
        raise ToolError("INVALID_PATH", "A file path is required.")
    return value


def sensitive_read(relative):
    parts = relative.lower().split("/")
    name = parts[-1]
    if (
        SENSITIVE_DIRS.intersection(parts)
        or name == ".env"
        or name.startswith(".env.")
        or name in SENSITIVE_FILES
        or re.search(r"\.(?:key|p12|pfx|pem)$", name)
        or re.search(
            r"(?:^|[-_.])(?:private[-_.]?key|service[-_.]?account|secrets?)(?:[-_.]|$)", name
        )
    ):
        raise ToolError(
            "SENSITIVE_READ_PATH_DENIED", "Reading sensitive workspace paths is not permitted."
        )


def sensitive_write(relative):
    parts = relative.lower().split("/")
    name = parts[-1]
    if relative.startswith(("app/api/", "src/app/api/", "pages/api/", "src/pages/api/")):
        raise ToolError(
            "PLATFORM_PATH_READ_ONLY",
            "API execution belongs to Python; use the platform-owned /api/market rewrite.",
        )
    if parts[0] == ".data-agent":
        raise ToolError(
            "PLATFORM_PATH_READ_ONLY",
            "Data Agent control directories are platform-owned and read-only.",
        )
    if (
        WRITE_DIRS.intersection(parts)
        or name in LOCK_FILES
        or name == ".env"
        or name.startswith(".env.")
    ):
        raise ToolError("SENSITIVE_PATH_DENIED", "Writing to this workspace path is not permitted.")
    if CONFIG.fullmatch(name):
        raise ToolError(
            "EXECUTABLE_CONFIG_DENIED", "Agent tools cannot modify executable build configuration."
        )


def default_write(relative):
    if relative in {"src/App.vue", "src/style.css"}:
        return True
    if relative.startswith(("src/components/", "src/hooks/", "src/lib/", "src/styles/", "src/types/")):
        return bool(re.search(r"\.(css|scss|sass|less|js|ts|vue)$", relative, re.I))
    if relative.startswith("public/"):
        return bool(
            re.search(
                r"\.(avif|bmp|css|gif|ico|jpe?g|json|png|svg|webp|woff2?|ttf|otf)$", relative, re.I
            )
        )
    return False


def no_symlinks(root, target):
    if not target.is_relative_to(root):
        raise ToolError("PATH_TRAVERSAL_DENIED", "Path escaped the workspace.")
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ToolError(
                "SYMLINK_WRITE_DENIED", "Tools cannot write through a symbolic-link component."
            )
        if not current.exists():
            return


@dataclass(frozen=True)
class ResolvedPath:
    absolute: Path
    canonical: Path
    relative: str
    canonical_relative: str
    exists: bool


class WorkspacePolicy:
    def __init__(self, root, allowed_write_globs=(), *, include_default=True):
        try:
            self.root = Path(root).resolve(strict=True)
            if not self.root.is_dir():
                raise ValueError("Not a directory")
        except (ValueError, OSError) as error:
            raise ToolError(
                "INVALID_WORKSPACE", "Workspace must be an existing directory."
            ) from error
        self.allowed = tuple(allowed_write_globs)
        self.include_default = include_default

    def read(self, relative, *, allow_root=False):
        relative = normalize(relative, allow_root)
        absolute = self.root / relative
        try:
            canonical = absolute.resolve(strict=True)
        except FileNotFoundError as error:
            raise ToolError("PATH_NOT_FOUND", "Workspace path does not exist.") from error
        if not canonical.is_relative_to(self.root):
            raise ToolError("SYMLINK_ESCAPE_DENIED", "Workspace path resolves outside its root.")
        canonical_relative = canonical.relative_to(self.root).as_posix()
        sensitive_read(relative)
        sensitive_read(canonical_relative)
        return ResolvedPath(absolute, canonical, relative, canonical_relative, True)

    def write(self, relative):
        relative = normalize(relative)
        absolute = Path(os.path.abspath(self.root / relative))
        sensitive_write(relative)
        canonical = absolute.resolve()
        if not canonical.is_relative_to(self.root):
            raise ToolError("SYMLINK_ESCAPE_DENIED", "Workspace path resolves outside its root.")
        canonical_relative = canonical.relative_to(self.root).as_posix()
        sensitive_write(canonical_relative)
        no_symlinks(self.root, absolute)
        if absolute.is_dir():
            raise ToolError("NOT_A_FILE", "Expected a file path, received a directory.")

        def allowed(path):
            return (self.include_default and default_write(path)) or any(
                matches_glob(path, glob) for glob in self.allowed
            )

        if not allowed(relative) or not allowed(canonical_relative):
            raise ToolError(
                "WRITE_PATH_DENIED", "Agent profile does not allow writing to this path."
            )
        return ResolvedPath(absolute, canonical, relative, canonical_relative, absolute.exists())
