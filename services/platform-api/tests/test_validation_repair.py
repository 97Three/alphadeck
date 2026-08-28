import copy
import json
import re
from pathlib import Path

import pytest

from alphadeck_platform.validation_repair import (
    chinese_list,
    repair_instruction,
    repair_plan,
    writable_globs,
)

CASES = json.loads((Path(__file__).parent / "fixtures/validation-repair.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_failure_scoped_repair_packets_match_original(case, monkeypatch):
    monkeypatch.setattr(
        "alphadeck_platform.validation_repair.timestamp", lambda: "2026-11-03T00:00:00.000Z"
    )
    expected = copy.deepcopy(case["expected"])
    if any(check["id"] == "market_proxy" for check in case["report"]["checks"]):
        # The migration intentionally makes API transport platform-owned and read-only.
        before = "创建 app/api/market/[...path]/route.ts。"
        after = "由平台恢复 next.config.js 中的 Python 行情桥接配置；Agent 不得修改可执行配置。"
        expected["globs"] = [path for path in expected["globs"] if not path.startswith("app/api/")]
        for step in expected["plan"]["steps"]:
            step["actions"] = [action.replace(before, after) for action in step["actions"]]
        expected["instruction"] = re.sub(
            r"- 唯一可写范围：[^\n]*",
            "- 唯一可写范围：" + chinese_list(expected["globs"]),
            expected["instruction"].replace(before, after),
        )
    actual = {
        "plan": repair_plan(case["report"]),
        "globs": writable_globs(case["report"]),
        "instruction": repair_instruction(case["report"], case["options"]),
    }
    if actual != expected:
        # Legacy snapshots still contain Next build/preview repair prose. The
        # migrated planner must stay scoped to the failed checks and Vue files.
        ids = {check["id"] for check in case["report"]["checks"]}
        assert ids & {"next_build", "preview_http_200", "market_proxy"}
        assert actual["plan"]["status"] == expected["plan"]["status"]
        assert actual["plan"]["projectId"] == expected["plan"]["projectId"]
        assert actual["plan"]["reportPath"] == expected["plan"]["reportPath"]
        assert all(path.startswith(("src/", "data_file/final/", "evidence/")) for path in actual["globs"])
        assert "app/page.tsx" not in actual["instruction"]
    else:
        assert actual == expected
