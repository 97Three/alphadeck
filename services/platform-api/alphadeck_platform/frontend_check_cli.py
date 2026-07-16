"""Explicit frontend integration entry point; fixtures never call paid upstreams."""

import os
import subprocess
import sys

if __name__ == "__main__":
    environment = {**os.environ, "PLATFORM_FRONTEND_E2E": "1"}
    if not (
        environment.get("PLATFORM_TEST_DATABASE_URL")
        or environment.get("PI_AGENT_TEST_DATABASE_URL")
    ):
        raise SystemExit(
            "Set PLATFORM_TEST_DATABASE_URL to an isolated _test database before the browser integration check"
        )
    raise SystemExit(
        subprocess.call(
            [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "tests/test_frontend_e2e.py",
                "-q",
                *sys.argv[1:],
            ],
            env=environment,
        )
    )
