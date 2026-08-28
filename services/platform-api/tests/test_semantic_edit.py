import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from alphadeck_platform.file_tools import FileTools, ToolContext
from alphadeck_platform.semantic_edit import SemanticEditTool, apply_edit
from alphadeck_platform.tool_paths import ToolError

CASES = json.loads((Path(__file__).parent / "fixtures/semantic-edit.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_semantic_edit_preserves_original_source_contract(case):
    try:
        result = {"result": apply_edit(case["input"], case["content"])}
    except ToolError as error:
        result = {
            "error": {
                "code": error.code,
                "message": error.message,
                **({"details": error.details} if error.details is not None else {}),
            }
        }
    assert result == case["expected"]


def test_semantic_edit_checks_source_hash_and_commits_once(tmp_path):
    async def run():
        content = "export function compute() { return 1; }\n"
        target = tmp_path / "src/lib/math.ts"
        target.parent.mkdir(parents=True)
        target.write_text(content)
        input = {
            "path": "src/lib/math.ts",
            "kind": "typescript_symbol",
            "symbol": "compute",
            "replacement": "export function compute() { return 2; }",
            "beforeSha256": hashlib.sha256(content.encode()).hexdigest(),
        }
        commits = []

        async def commit(operation):
            commits.append(True)
            return await operation()

        tool = SemanticEditTool(FileTools(tmp_path).policy)
        result = await tool.execute(input, ToolContext("run", "semantic", commit))
        assert result["ok"], result
        assert target.read_text() == "export function compute() { return 2; }\n"
        assert len(commits) == 1
        stale = await tool.execute(input, ToolContext("run", "stale", commit))
        assert stale["error"]["code"] == "WORKSPACE_WRITE_CONFLICT"
        assert len(commits) == 1

    asyncio.run(run())


def test_vue_script_symbol_edit_preserves_template_and_rejects_invalid_code():
    content = '<script setup lang="ts">\nconst answer = 1;\n</script>\n<template><main>{{ answer }}</main></template>\n'
    input = {
        "path": "src/App.vue",
        "kind": "typescript_symbol",
        "symbol": "answer",
        "replacement": "const answer = 2;",
    }
    changed = apply_edit(input, content)["content"]
    assert "const answer = 2;" in changed
    assert "<template><main>{{ answer }}</main></template>" in changed
    with pytest.raises(ToolError, match="syntactically valid"):
        apply_edit({**input, "replacement": "const answer = ;"}, content)
