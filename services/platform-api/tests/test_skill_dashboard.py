import copy
import json
import shutil
from pathlib import Path

from alphadeck_platform.config import ROOT
from alphadeck_platform.skill_dashboard import dashboard


def stable(value):
    if isinstance(value, dict):
        return {
            key: stable(item)
            for key, item in value.items()
            if key not in ("generatedAt", "updatedAt")
        }
    return [stable(item) for item in value] if isinstance(value, list) else value


def test_skills_dashboard_matches_all_original_source_projections():
    expected = json.loads((Path(__file__).parent / "fixtures/skills-dashboard.json").read_text())
    actual = stable(dashboard(ROOT))
    assert actual["totals"] == expected["totals"]
    assert [item["id"] for item in actual["skills"]] == [item["id"] for item in expected["skills"]]
    assert all(item["health"]["status"] == "ok" for item in actual["skills"])
    assert all(item["source"] and item["package"] for item in actual["skills"])


def test_skills_dashboard_detects_source_and_package_drift_without_activating_it(tmp_path):
    for relative in (".pi/skills", ".pi/skill-packages"):
        shutil.copytree(ROOT / relative, tmp_path / relative)
    for name in ("skills.registry.json", "skills.lock.json", "skills.changelog.json"):
        shutil.copy2(ROOT / ".pi" / name, tmp_path / ".pi" / name)
    before = copy.deepcopy(dashboard(tmp_path))
    target = tmp_path / ".pi/skills/query-rewrite/SKILL.md"
    target.write_text(target.read_text() + "\n# Unsaved source change\n")
    package = tmp_path / ".pi/skill-packages/query-rewrite.tgz"
    package.write_bytes(package.read_bytes() + b"modified")
    after = dashboard(tmp_path)
    item = next(item for item in after["skills"] if item["id"] == "query-rewrite")
    assert item["health"] == {
        "status": "warning",
        "missing": ["source_hash", "package_hash"],
        "sourceChanged": True,
        "packageChanged": True,
        "versionMismatch": False,
    }
    assert before["totals"]["warning"] + 1 == after["totals"]["warning"]
