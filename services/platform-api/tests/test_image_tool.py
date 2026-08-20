import asyncio
import base64
import json
from pathlib import Path

import pytest

from alphadeck_platform.image_tool import ImageTool, parse_image_input
from alphadeck_platform.tool_paths import ToolError, WorkspacePolicy

FIXTURE = json.loads((Path(__file__).parent / "fixtures/image-tool.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_image_tool_metadata_magic_dimensions_and_limits_match_original(
    case, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "alphadeck_platform.image_tool.timestamp", lambda: "2026-11-03T00:00:00.000Z"
    )
    for relative, encoded in FIXTURE["images"].items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(encoded))
    context = tmp_path / ".data-agent/attachments.json"
    context.parent.mkdir()
    context.write_text(json.dumps({"attachments": case.get("attachments", [])}))

    async def run():
        names = {
            "maxAttachments": "max_attachments",
            "maxImageBytes": "max_image_bytes",
            "maxOutputChars": "max_output_chars",
        }
        tool = ImageTool(
            WorkspacePolicy(tmp_path),
            **{names[key]: value for key, value in case.get("options", {}).items()},
        )
        try:
            value = parse_image_input(case["input"])
            actual = await tool.execute(value)
        except ToolError as error:
            actual = {"parseError": {"code": error.code, "message": error.message}}
        assert actual == case["expected"]

    asyncio.run(run())
