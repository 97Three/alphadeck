"""Fingerprint deployed executable sources without reading credentials or user workspaces."""

import hashlib
import os
import re
import subprocess
from functools import lru_cache

from .config import ROOT
from .provenance import frame
from .skill_integrity import read_regular

FRAMEWORK_VERSION = "python-1"


@lru_cache(maxsize=1)
def build_identity():
    digest = hashlib.sha256()
    sources = []
    for relative, pattern in (
        ("services/platform-api/alphadeck_platform", "*.py"),
        ("config", "*.json"),
    ):
        folder = ROOT / relative
        sources.extend(path for path in folder.rglob(pattern) if path.is_file())
    sources.extend(
        ROOT / relative
        for relative in (
            "services/platform-api/app.py",
            "services/platform-api/pyproject.toml",
            "services/platform-api/uv.lock",
        )
    )
    total = 0
    for path in sorted(sources):
        content = read_regular(path, 4194304)
        total += len(content)
        if total > 33554432:
            raise ValueError("Backend source fingerprint exceeds its complete-input budget")
        frame(digest, path.relative_to(ROOT).as_posix(), content)
    configured = (
        os.getenv("PI_AGENT_BUILD_REVISION") or os.getenv("ALPHADECK_BUILD_REVISION", "")
    ).strip()
    if configured and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,191}", configured):
        raise ValueError("Invalid ALPHADECK_BUILD_REVISION")
    git = next(
        (
            value.lower()
            for key in ("VERCEL_GIT_COMMIT_SHA", "GITHUB_SHA", "CI_COMMIT_SHA", "SOURCE_VERSION")
            if re.fullmatch(r"[a-fA-F0-9]{7,64}", value := os.getenv(key, "").strip())
        ),
        None,
    )
    if git is None and (ROOT / ".git").exists():
        try:
            output = subprocess.run(
                ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout.strip()
            git = output.lower() if re.fullmatch(r"[a-fA-F0-9]{7,64}", output) else None
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "frameworkVersion": FRAMEWORK_VERSION,
        "buildRevision": configured or "python-sha256:" + digest.hexdigest(),
        "gitRevision": git,
        "sourceSha256": digest.hexdigest(),
    }
