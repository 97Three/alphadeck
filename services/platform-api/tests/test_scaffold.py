import json

import pytest

from alphadeck_platform.scaffold import CATALOG, ensure_scaffold


def test_vue_scaffold_is_complete_and_idempotent(tmp_path):
    ensure_scaffold(tmp_path, "research", shared_modules=False)
    expected = set(CATALOG["files"]) | {"package.json"}
    files = {
        path.relative_to(tmp_path).as_posix(): path.read_text()
        for path in tmp_path.rglob("*") if path.is_file()
    }
    assert set(files) == expected
    assert files["src/App.vue"].startswith("<script setup")
    assert "vue" in json.loads(files["package.json"])["dependencies"]
    assert "next" not in json.loads(files["package.json"])["dependencies"]
    mtimes = {relative: (tmp_path / relative).stat().st_mtime_ns for relative in files}
    ensure_scaffold(tmp_path, "research", shared_modules=False)
    assert {relative: (tmp_path / relative).stat().st_mtime_ns for relative in files} == mtimes


def test_vue_scaffold_preserves_research_page_and_replaces_platform_proxy(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/App.vue").write_text("<template>我的研究</template>")
    (tmp_path / "vite.config.ts").write_text("unsafe proxy")
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "old", "scripts": {"custom": "echo ok"},
        "dependencies": {"react": "1", "next": "1", "my-chart": "2"},
    }))
    ensure_scaffold(tmp_path, "research", shared_modules=False)
    assert (tmp_path / "src/App.vue").read_text() == "<template>我的研究</template>"
    assert (tmp_path / "vite.config.ts").read_text() == CATALOG["files"]["vite.config.ts"]
    package = json.loads((tmp_path / "package.json").read_text())
    assert package["name"] == "research"
    assert "react" not in package["dependencies"]
    assert "next" not in package["dependencies"]
    assert package["dependencies"]["my-chart"] == "2"
    assert package["scripts"]["custom"] == "echo ok"
    ensure_scaffold(tmp_path, "research", shared_modules=False, restore=True)
    assert (tmp_path / "src/App.vue").read_text() == CATALOG["files"]["src/App.vue"]


def test_vue_scaffold_cannot_follow_a_control_file_symlink(tmp_path):
    (tmp_path / "private.txt").write_text("private")
    (tmp_path / "package.json").symlink_to(tmp_path / "private.txt")
    with pytest.raises(ValueError, match="links"):
        ensure_scaffold(tmp_path, "project", shared_modules=False)
    assert (tmp_path / "private.txt").read_text() == "private"
