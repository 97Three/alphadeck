import os
import shutil
import sys
from pathlib import Path

from .config import ROOT

SAFE = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "SystemRoot", "ComSpec", "PATHEXT")
INSTALL = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "npm_config_registry",
    "npm_config_cache",
)
OVERRIDES = (
    "PORT",
    "WEB_PORT",
    "NEXT_PUBLIC_APP_URL",
    "NODE_ENV",
    "NEXT_PRIVATE_BUILD_WORKER",
    "ALPHADECK_SANDBOX_PREVIEW_SOCKET",
    "ALPHADECK_SANDBOX_PREVIEW_PORT",
    "ALPHADECK_SANDBOX_MARKET_SOCKET",
    "ALPHADECK_SANDBOX_MARKET_PORT",
)


def generated_env(workspace, overrides=None, *, install=False, environment=None):
    overrides = overrides or {}
    environment = os.environ if environment is None else environment
    result = {
        "CI": overrides.get("CI", "1"),
        "NEXT_TELEMETRY_DISABLED": "1",
        "ALPHADECK_WORKSPACE_ROOT": str(Path(workspace).resolve()),
    }
    for key in SAFE + (INSTALL if install else ()):
        value = overrides.get(key, environment.get(key))
        if value:
            result[key] = value
    for key in OVERRIDES:
        if overrides.get(key):
            result[key] = overrides[key]
    return result


def namespace_enabled():
    return sys.platform == "linux" and os.getenv("ALPHADECK_GENERATED_SANDBOX") != "0"


def wrap_command(workspace, command, arguments, *, root=ROOT):
    if not namespace_enabled():
        if os.getenv("ALPHADECK_ALLOW_UNSANDBOXED_GENERATED_CODE") == "1":
            return [command, *arguments]
        raise RuntimeError("Generated project execution requires the Linux namespace sandbox.")
    workspace = Path(workspace).resolve(strict=True)
    modules = workspace / "node_modules"
    modules = (
        modules.resolve(strict=True)
        if modules.exists()
        else (root / "web/node_modules").resolve(strict=True)
    )
    if modules not in {workspace / "node_modules", (root / "web/node_modules").resolve()}:
        raise RuntimeError(
            "Generated project modules must belong to its workspace or the platform."
        )
    executable = shutil.which("node")
    if not executable:
        raise RuntimeError("Generated project sandbox cannot locate a trusted Node.js runtime.")
    runtime = Path(executable).resolve(strict=True).parent.parent
    runner = root / "scripts/security/run-generated-project-python-sandbox.sh"
    if not runner.is_file():
        raise RuntimeError("Python preview sandbox launcher is missing.")
    return [
        "unshare",
        "--user",
        "--map-root-user",
        "--mount",
        "--net",
        "--pid",
        "--fork",
        "/bin/bash",
        str(runner),
        str(workspace),
        str(modules),
        str(runtime),
        "--",
        command,
        *arguments,
    ]
