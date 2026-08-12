import asyncio
import json
from pathlib import Path

import pytest

from alphadeck_platform.dashboard_inspector import CATALOG, DashboardInspector
from alphadeck_platform.tool_paths import WorkspacePolicy

CASES = json.loads((Path(__file__).parent / "fixtures/dashboard-inspector.json").read_text())


@pytest.mark.parametrize(
    "case", CASES, ids=lambda case: case["name"] + "_" + str(case["maxOutputChars"])
)
def test_dashboard_inspection_and_bounded_reports_match_original(case, tmp_path):
    for relative, content in case["files"].items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    async def run():
        tool = DashboardInspector(
            WorkspacePolicy(tmp_path),
            max_output_chars=case["maxOutputChars"],
            max_file_bytes=1024 if case["name"] == "binary" else 2000000,
        )
        result = await tool.execute({})
        assert result["ok"] is True
        assert result["data"]["schemaVersion"] == 1
        expected_files = sum(
            any(path in case["files"] for path in candidates)
            for candidates in CATALOG["candidates"].values()
        )
        assert result["data"]["inspectedFiles"] == expected_files
        assert result["data"]["contentTruncated"] == case["expected"]["data"]["contentTruncated"]
        assert len(result["content"]) <= case["maxOutputChars"]
        if not result["data"]["contentTruncated"]:
            report = json.loads(result["content"])
            assert report["schemaVersion"] == 1
            assert set(report["files"]) == set(CATALOG["candidates"])
            assert report["missingRequiredFiles"] == result["data"]["missingRequiredFiles"]
            assert report["files"]["marketProxy"]["path"] == "vite.config.ts"

    asyncio.run(run())
