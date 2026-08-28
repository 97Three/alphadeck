import asyncio
import copy
import json
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from alphadeck_platform.config import ROOT
from alphadeck_platform.database import Database
from alphadeck_platform.prefetch_transport import WorkspaceArtifacts
from alphadeck_platform.preview import PreviewManager
from alphadeck_platform.projects import Projects
from alphadeck_platform.validation_runtime import (
    GeneratedCommandRunner,
    check_build,
    check_preview,
    check_visual,
    normalize_generated,
    prepare_validation,
)


def test_generated_command_timeout_cleans_up_and_retains_output(tmp_path):
    async def run():
        runner = GeneratedCommandRunner(
            lambda root, args: [
                sys.executable,
                "-c",
                "import time; print('started', flush=True); time.sleep(30)",
            ]
        )
        result = await runner.run(tmp_path, [], timeout=0.1)
        assert result["timedOut"] and "started" in result["output"]
        assert result["signal"] in {"SIGTERM", "SIGKILL"}

    asyncio.run(run())


def test_real_next_build_and_preview_are_driven_by_python(
    tmp_path, monkeypatch, database_url, actor
):
    if sys.platform != "linux" or not (ROOT / "node_modules/next/package.json").is_file():
        pytest.skip("Requires Linux namespace support and frontend dependencies")

    project, projects_root = "next-fixture-" + uuid4().hex, tmp_path
    tmp_path = tmp_path / project
    tmp_path.mkdir()
    with psycopg.connect(database_url) as tx:
        tx.execute(
            "INSERT INTO projects (id,name,owner_id,repo_path,updated_at) VALUES (%s,'Next validation',%s,%s,now())",
            (project, actor["user_id"], str(tmp_path)),
        )

    async def run():
        async def authority(operation):
            return await operation(None)

        artifacts = WorkspaceArtifacts(tmp_path, authority)
        source = json.loads((Path(__file__).parent / "fixtures/data-prefetch.json").read_text())[0]
        for relative, value in source["expected"]["entries"].items():
            await artifacts.write(relative, copy.deepcopy(value))
        plan = source["plan"]
        plan["visualization"] = {"templateId": "single-stock-diagnosis"}
        await artifacts.write(".data-agent/finance-run-plan.json", plan)
        await prepare_validation(project, artifacts, settle=False)
        before = (tmp_path / "package.json").stat().st_mtime_ns
        normalize_generated(tmp_path)
        assert (tmp_path / "package.json").stat().st_mtime_ns == before
        build = await check_build(artifacts, GeneratedCommandRunner(), timeout=240)
        assert build["status"] == "passed", build

        previews = PreviewManager(
            Projects(Database(database_url), projects_root), readiness_seconds=45
        )
        try:
            http = await check_preview(project, previews)
            assert http["status"] == "passed", http
            visual = await check_visual(project, artifacts, previews, "fixture-run")
            assert (tmp_path / ".data-agent/visual-validation.json").is_file()
            assert len(visual["metadata"]["viewports"]) == 2, visual
        finally:
            await previews.cleanup_all()

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as tx:
            tx.execute("DELETE FROM projects WHERE id=%s", (project,))
