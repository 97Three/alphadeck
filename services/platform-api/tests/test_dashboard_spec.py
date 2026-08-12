import asyncio
import copy
import json
from pathlib import Path

import pytest

from alphadeck_platform.dashboard_spec import (
    CATALOG,
    DashboardSpecTool,
    assess_readiness,
    compile_dashboard_spec,
)
from alphadeck_platform.file_tools import FileTools, ToolContext
from alphadeck_platform.tool_paths import ToolError

CASES = json.loads((Path(__file__).parent / "fixtures/dashboard-spec.json").read_text())


def test_base_renderer_exposes_candle_details_without_raw_backend_objects():
    page = CATALOG["renders"]["base"]["page"]
    assert '@pointermove="selectBarAtPointer"' in page
    assert "开 {{ format(focusedBar.open) }}" in page
    assert "成交额 {{ formatAmount(focusedBar.amount) }}" in page
    assert "const metricRows = computed" in page
    assert 'v-for="(value, key) in metrics"' not in page
    assert "JSON.stringify(value)" not in page
    assert "JSON.stringify(realTime)" not in page


def vue_artifacts(value):
    value = copy.deepcopy(value)
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "outputArtifacts" and nested == ["app/page.tsx", "app/globals.css"]:
                value[key] = ["src/App.vue", "src/style.css"]
            else:
                value[key] = vue_artifacts(nested)
    elif isinstance(value, list):
        value = [vue_artifacts(item) for item in value]
    return value


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_dashboard_compiler_and_preflight_match_original(case):
    try:
        actual = {
            "spec": compile_dashboard_spec(case["runPlan"], case["finalData"], case["assertion"])
        }
    except ToolError as error:
        actual = {
            "error": {
                "code": error.code,
                "message": error.message,
                **({"details": error.details} if error.details is not None else {}),
            }
        }
    assert actual == vue_artifacts(case["expected"])
    assert assess_readiness(case["runPlan"], case["finalData"]) == vue_artifacts(case["readiness"])


def test_dashboard_compiler_uses_one_fenced_batch_and_never_writes_without_authority(tmp_path):
    async def run():
        case = next(case for case in CASES if case["name"] == "singleStockData_valid")
        for relative, value in (
            (".data-agent/finance-run-plan.json", case["runPlan"]),
            ("data_file/final/dashboard-data.json", case["finalData"]),
        ):
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
        files = FileTools(tmp_path)
        tool = DashboardSpecTool(files.policy)
        denied = await tool.execute({}, None)
        assert denied["error"]["code"] == "WORKSPACE_COMMIT_FENCE_REQUIRED"
        assert not (tmp_path / "src").exists()
        commits = []

        async def physical_commit(operation):
            commits.append(True)
            return await operation()

        result = await tool.execute({}, ToolContext("run", "op", physical_commit))
        assert result["ok"], result
        assert len(commits) == 1 and len(result["data"]["files"]) == 2
        assert (tmp_path / "src/App.vue").read_text() == CATALOG["renders"]["base"]["page"]
        assert (tmp_path / "src/style.css").read_text() == CATALOG["renders"]["base"]["styles"]
        changed = copy.deepcopy(case["runPlan"])
        changed["runId"] = "other-run"
        (tmp_path / ".data-agent/finance-run-plan.json").write_text(json.dumps(changed))
        result = await tool.execute({}, ToolContext("run", "op2", physical_commit))
        assert result["error"]["code"] == "DASHBOARD_SPEC_DATA_IDENTITY_MISMATCH"
        assert len(commits) == 1

    asyncio.run(run())
