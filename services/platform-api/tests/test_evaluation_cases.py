import asyncio
import json
from pathlib import Path

import pytest

from alphadeck_platform.evaluation_cases import EvaluationCases, normalize_case, validate_oracles
from alphadeck_platform.evaluation_oracles import evaluate

CASES = json.loads((Path(__file__).parent / "fixtures/evaluation-cases.json").read_text())


@pytest.mark.parametrize("case", CASES["cases"])
def test_case_catalogue_projection_and_tags_match_original(case):
    assert normalize_case(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["oracles"])
def test_data_and_safety_assertions_match_original(case):
    assert evaluate(case["input"]["assertions"], case["input"]["targets"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["validations"])
def test_invalid_oracles_cannot_be_saved_as_production_checks(case):
    if not case["ok"]:
        with pytest.raises(ValueError):
            validate_oracles(case["input"])
    else:
        assert validate_oracles(case["input"]) == case["expected"]


def test_case_and_set_edits_are_serialized_without_losing_concurrent_additions(tmp_path):
    catalog = EvaluationCases(tmp_path)

    async def run():
        inputs = [
            {
                "id": f"case-{index}",
                "name": f"Test {index}",
                "question": "分析600519",
                "capabilityId": "stock_diagnosis",
            }
            for index in range(4)
        ]
        created = await asyncio.gather(*(catalog.create(value) for value in inputs))
        assert {item["id"] for item in catalog.list()} == {item["id"] for item in created}
        with pytest.raises(ValueError, match="已存在"):
            await catalog.create(inputs[0])
        with pytest.raises(ValueError, match="至少提供一条"):
            await catalog.create({**inputs[0], "id": "unsupported", "productionSupported": True})
        with pytest.raises(ValueError, match="已存在"):
            await catalog.create_set(
                {"id": "all", "name": "Reserved", "caseIds": [created[0]["id"]]}
            )
        saved = await catalog.create_set(
            {"id": "custom:test", "name": "Subset", "caseIds": [created[0]["id"], "does-not-exist"]}
        )
        assert saved["caseIds"] == [created[0]["id"]]
        assert catalog.sets() == [saved]
        assert (
            "does-not-exist"
            not in json.loads((tmp_path / "benchmarks/alphadeck/eval-sets.json").read_text())[0][
                "caseIds"
            ]
        )

    asyncio.run(run())
