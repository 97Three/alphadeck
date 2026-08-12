import json
from pathlib import Path

import pytest

from alphadeck_platform.artifact_policy import check_artifact_policy

CASES = json.loads((Path(__file__).parent / "fixtures/artifact-policy.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_artifact_execution_policy_matches_original(tmp_path, case):
    for relative, content in case["files"].items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    actual = check_artifact_policy(tmp_path)
    backend_files = [path for path in case["files"] if path.startswith("app/api/")]
    if backend_files:
        assert actual["status"] == "failed"
        assert all(path + " 包含生成的后端接口" in actual["details"] for path in backend_files)
        for detail in case["expected"].get("details", "").splitlines():
            assert detail in actual["details"]
    else:
        assert actual == case["expected"]


def test_artifact_execution_policy_refuses_large_or_linked_source(tmp_path):
    (tmp_path / "app").mkdir()
    page = tmp_path / "app/page.tsx"
    page.write_text("x" * 300001)
    with pytest.raises(ValueError):
        check_artifact_policy(tmp_path)
    page.unlink()
    (tmp_path / "private.txt").write_text("private")
    page.symlink_to(tmp_path / "private.txt")
    with pytest.raises(ValueError, match="symbolic links"):
        check_artifact_policy(tmp_path)
