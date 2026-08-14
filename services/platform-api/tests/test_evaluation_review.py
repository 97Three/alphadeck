import asyncio
import copy
import json
from pathlib import Path

import pytest

from alphadeck_platform.agent_reviewer import parse_review, review_workspace
from alphadeck_platform.evaluation_evaluators import apply_evaluator, is_current
from alphadeck_platform.provenance import js_json
from alphadeck_platform.review_evidence import ARTIFACTS, load_evidence, project, resolves

CASES = json.loads((Path(__file__).parent / "fixtures/evaluation-review.json").read_text())


@pytest.mark.parametrize("case", CASES["evaluations"])
def test_evaluator_dimensions_and_completion_checks_match_original(case):
    input = case["input"]
    result = apply_evaluator(
        input["evaluatorId"], input["mode"], input["result"], input["semanticReview"]
    )
    assert result == case["expected"]
    assert is_current(result) == case["current"]
    changed = copy.deepcopy(result)
    changed["dimensions"][0]["weight"] += 1
    assert not is_current(changed)


@pytest.mark.parametrize("case", CASES["reviews"])
def test_reviewer_requires_correct_dimensions_and_verifiable_citations(case):
    input = case["input"]
    if "error" in case:
        with pytest.raises(ValueError):
            parse_review(input["value"], input["usage"], input["reviewer"], input["evidence"])
    else:
        assert (
            parse_review(input["value"], input["usage"], input["reviewer"], input["evidence"])
            == case["expected"]
        )


@pytest.mark.parametrize("case", CASES["pointers"])
def test_citations_resolve_only_the_exact_projected_evidence(case):
    assert resolves(case["reference"], case["evidence"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["projections"])
def test_review_projection_keeps_head_and_tail_and_marks_omitted_values(case):
    assert project(case["input"], case["limit"]) == case["expected"]


def test_reviewer_stream_stays_read_only_and_rejects_missing_finish(tmp_path):
    fixture = CASES["reviews"][0]["input"]
    for identity, relative in ARTIFACTS.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(js_json(fixture["evidence"][identity]["value"]))
    requests = []

    class Provider:
        def __init__(self, finish=True):
            self.finish = finish

        async def complete(self, request):
            requests.append(request)
            yield {"type": "text_delta", "delta": fixture["value"]}
            yield {"type": "usage", "usage": fixture["usage"]}
            if self.finish:
                yield {"type": "finish", "reason": "stop"}

    async def run():
        review = await review_workspace(
            tmp_path, "分析600519", {"id": "fixture"}, {"passed": True}, provider=Provider()
        )
        assert review["verdict"] == "passed"
        assert requests[0]["toolChoice"] == "none" and "tools" not in requests[0]
        assert requests[0]["reasoning"]["enabled"] is False
        assert review["reviewer"]["independentFromGenerator"] is False
        with pytest.raises(ValueError, match="完成信号"):
            await review_workspace(tmp_path, "分析600519", {}, {}, provider=Provider(False))

    asyncio.run(run())
    final = tmp_path / ARTIFACTS["finalData"]
    original = final.read_text()
    final.unlink()
    source = tmp_path / "other.json"
    source.write_text(original)
    final.symlink_to(source)
    with pytest.raises(ValueError, match="unsafe"):
        load_evidence(tmp_path)
