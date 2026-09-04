import asyncio
import json
from pathlib import Path

import pytest

from alphadeck_platform.provenance import canonical_json, js_json
from alphadeck_platform.structured_tools import StructuredFileTools

CASES = json.loads((Path(__file__).parent / "fixtures/structured-tools.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["scenario"]["name"])
def test_structured_read_matches_original_bounded_projections(case, tmp_path):
    for relative, content in case["files"].items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
    scenario = case["scenario"]
    tools = StructuredFileTools(
        tmp_path,
        finance=scenario.get("finance", False),
        max_output=scenario.get("maxOutputChars", 12000),
    )
    result = asyncio.run(
        tools.execute(
            "query_text_file" if scenario.get("text") else "query_json", scenario["input"]
        )
    )
    if case["expected"]["ok"]:
        assert result == case["expected"]
    else:
        assert not result["ok"]
        assert result["error"]["code"] == case["expected"]["error"]["code"]
        assert result["error"].get("details") == case["expected"]["error"].get("details")


def test_javascript_json_number_property_order_and_lone_surrogates_are_preserved():
    value = {"b": 1.0, "2": "two", "1": "one", "text": "\ud83d", "negativeZero": -0.0}
    assert js_json(value) == '{"1":"one","2":"two","b":1,"text":"\\ud83d","negativeZero":0}'
    assert (
        canonical_json(value).decode()
        == '{"1":"one","2":"two","b":1,"negativeZero":0,"text":"\\ud83d"}'
    )
