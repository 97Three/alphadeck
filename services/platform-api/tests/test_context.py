import json
from copy import deepcopy
from pathlib import Path

import pytest

from alphadeck_platform.context_capsule import (
    CapsuleSession,
    ContextError,
    assert_capsule,
    collect_targets,
)
from alphadeck_platform.context_manager import ContextManager, estimate_tokens

FIXTURE = json.loads((Path(__file__).parent / "fixtures/context.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_context_matches_original_with_atomic_protocol_history(case):
    original = deepcopy(case["messages"])
    manager = ContextManager(**case["options"])
    if "error" in case["expected"]:
        with pytest.raises(ContextError) as error:
            manager.prepare(case["messages"], case.get("tools", []), case.get("preparation"))
        assert {"code": error.value.code, "details": error.value.details} == case["expected"][
            "error"
        ]
    else:
        assert (
            manager.prepare(case["messages"], case.get("tools", []), case.get("preparation"))
            == case["expected"]["result"]
        )
    assert case["messages"] == original


@pytest.mark.parametrize("case", FIXTURE["capsules"], ids=lambda case: str(case["maximum"]))
def test_capsule_rollup_and_receipt_invalidation_match_original(case):
    session = CapsuleSession(case["maximum"])
    for operation in case["operations"]:
        session.record(operation)
    session.record_framework_outcome(case["outcome"])
    assert session.checkpoint("exploration") is None
    checkpoint = session.checkpoint("writing")
    assert checkpoint == case["expected"]
    if checkpoint:
        assert_capsule(checkpoint)
        assert "untrusted tool text" not in checkpoint["content"]
        forged = deepcopy(checkpoint)
        forged["coveredToolCallIds"].append("invented-call")
        with pytest.raises(ContextError, match="verification failed"):
            assert_capsule(forged)
        forged = deepcopy(checkpoint)
        forged["telemetry"]["successfulWrites"] += 1
        with pytest.raises(ContextError):
            assert_capsule(forged)


def test_token_estimation_and_target_projection_match_original():
    assert collect_targets(*FIXTURE["targets"]["sources"]) == FIXTURE["targets"]["expected"]
    for case in FIXTURE["estimates"]:
        assert estimate_tokens(case["messages"], []) == case["expected"]


def test_bad_config_and_estimator_cannot_skip_context_budget():
    for options in ((10, 10, 10), (10, -1, 10), (True, 0, 10), (10, 0, 0)):
        with pytest.raises(ContextError):
            ContextManager(*options)
    for estimator in (lambda *_: -1, lambda *_: float("nan"), lambda *_: "bad"):
        with pytest.raises(ContextError) as error:
            ContextManager(100, 10, 90, tokenEstimator=estimator).prepare([])
        assert error.value.code == "TOKEN_ESTIMATION_FAILED"
