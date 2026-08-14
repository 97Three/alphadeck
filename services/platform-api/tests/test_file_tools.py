import asyncio
import json
import os
from pathlib import Path

import pytest

from alphadeck_platform.file_tools import FileTools, ToolContext, write_batch
from alphadeck_platform.mutation_journal import (
    MutationConflict,
    MutationJournal,
    list_journals,
    sha,
)
from alphadeck_platform.tool_paths import ToolError, WorkspacePolicy

CASES = json.loads((Path(__file__).parent / "fixtures/file-tools.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_file_tools_match_original_results_and_physical_files(case, tmp_path):
    for relative, content in case["files"].items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode())
    tools = FileTools(tmp_path, structured_json_globs=["data_file/**"])
    calls = []

    async def authorize(commit):
        calls.append(True)
        return await commit()

    async def run():
        result = await tools.execute(
            case["tool"],
            case["input"],
            ToolContext("run-test", "operation-" + case["name"], authorize),
        )
        expected = case["expected"]
        if case["name"] == "write_file-7":
            assert result["error"]["code"] == "WRITE_PATH_DENIED"
            assert not (tmp_path / "app/new.ts").exists()
            return
        if case["tool"] in {"list_files", "search_files"}:
            assert result["ok"] is True
            assert ".env" not in result["content"]
            assert "app/components/Chart.tsx" in result["content"]
            return
        if not expected["ok"]:
            assert result["ok"] is False and result["error"]["code"] == expected["error"]["code"]
        else:
            assert result == expected
        for relative, content in case["resulting"].items():
            assert (tmp_path / relative).read_bytes() == content.encode()

    asyncio.run(run())


def test_batch_rollback_restores_every_file_after_partial_commit(tmp_path, monkeypatch):
    (tmp_path / "src/lib").mkdir(parents=True)
    (tmp_path / "src/lib/one.ts").write_bytes(b"old one")
    (tmp_path / "src/lib/two.ts").write_bytes(b"old two")
    os.chmod(tmp_path / "src/lib/one.ts", 0o750)
    real_replace = os.replace

    def fail_second(source, target):
        if Path(source).parent.name == "staged" and Path(target).name == "two.ts":
            raise OSError("intentional second rename failure")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second)

    async def authorize(commit):
        return await commit()

    async def run():
        with pytest.raises(ToolError) as failure:
            await write_batch(
                WorkspacePolicy(tmp_path),
                [
                    {"path": "src/lib/one.ts", "content": b"new one"},
                    {"path": "src/lib/two.ts", "content": b"new two"},
                ],
                ToolContext("run", "batch", authorize),
            )
        assert failure.value.code == "WORKSPACE_MUTATION_ROLLED_BACK"

    asyncio.run(run())
    assert (tmp_path / "src/lib/one.ts").read_bytes() == b"old one"
    assert (tmp_path / "src/lib/two.ts").read_bytes() == b"old two"
    assert (tmp_path / "src/lib/one.ts").stat().st_mode & 0o777 == 0o750
    journals = list_journals(tmp_path)
    assert len(journals) == 1 and journals[0].manifest["state"] == "rolled_back"


def test_vue_page_write_requires_fence_and_old_react_path_is_denied(tmp_path):
    tools = FileTools(tmp_path)

    async def authorize(commit):
        return await commit()

    async def run():
        without_fence = await tools.execute("write_file", {"path": "src/App.vue", "content": "<template>ok</template>"})
        assert without_fence["error"]["code"] == "WORKSPACE_COMMIT_FENCE_REQUIRED"
        context = ToolContext("run", "vue-write", authorize)
        created = await tools.execute("write_file", {"path": "src/App.vue", "content": "<template>ok</template>"}, context)
        assert created["ok"] is True
        denied = await tools.execute("write_file", {"path": "app/page.tsx", "content": "old"}, ToolContext("run", "react-write", authorize))
        assert denied["error"]["code"] == "WRITE_PATH_DENIED"

    asyncio.run(run())
    assert (tmp_path / "src/App.vue").read_text() == "<template>ok</template>"


def journal_fixture(tmp_path):
    (tmp_path / "app").mkdir()
    files = []
    for name in ("one.ts", "two.ts"):
        target = tmp_path / "app" / name
        target.write_bytes(b"before")
        files.append(
            {
                "target": "app/" + name,
                "content": b"after",
                "existedBefore": True,
                "mode": 0o644,
                "beforeSha256": sha(b"before"),
                "afterSha256": sha(b"after"),
            }
        )
    return MutationJournal.prepare(tmp_path, "run", "crashed-operation", files)


def test_restart_recovers_partial_batch_but_never_overwrites_later_edits(tmp_path):
    journal = journal_fixture(tmp_path)
    journal.set_state("committing")
    os.replace(journal.directory / "staged/0", tmp_path / "app/one.ts")
    (tmp_path / "app/two.ts").write_bytes(b"later user edit")
    recovered = list_journals(tmp_path)[0]
    with pytest.raises(MutationConflict):
        recovered.rollback()
    assert (tmp_path / "app/one.ts").read_bytes() == b"after"
    assert (tmp_path / "app/two.ts").read_bytes() == b"later user edit"
    (tmp_path / "app/two.ts").write_bytes(b"before")
    recovered.rollback()
    assert (tmp_path / "app/one.ts").read_bytes() == b"before"
    assert recovered.manifest["state"] == "rolled_back"
    recovered.cleanup()
    assert list_journals(tmp_path) == []


def test_writes_require_database_authorization_and_reject_control_or_symlink_paths(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / ".data-agent").mkdir()
    (tmp_path / "app/alias").symlink_to(tmp_path / "app", target_is_directory=True)
    tools = FileTools(tmp_path, allowed_write_globs=["**"])

    async def run():
        missing = await tools.execute("write_file", {"path": "app/page.tsx", "content": "test"})
        assert missing["error"]["code"] == "WORKSPACE_COMMIT_FENCE_REQUIRED"

        async def authorize(commit):
            return await commit()

        context = ToolContext("run", "operation", authorize)
        control = await tools.execute(
            "write_file", {"path": ".data-agent/validation.json", "content": "{}"}, context
        )
        assert control["error"]["code"] == "PLATFORM_PATH_READ_ONLY"
        alias = await tools.execute(
            "write_file", {"path": "app/alias/page.tsx", "content": "test"}, context
        )
        assert alias["error"]["code"] == "SYMLINK_WRITE_DENIED"

    asyncio.run(run())
