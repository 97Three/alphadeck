import copy
import json
from pathlib import Path

import pytest

from alphadeck_platform.product_control_evidence import MODEL, PROVIDER, attest_product_controls


def valid():
    result = copy.deepcopy(
        json.loads((Path(__file__).parent / "fixtures/evaluation-attestation.json").read_text())[0][
            "value"
        ]
    )
    execution = result["agentExecution"]
    for container in (execution, execution["runs"][0]):
        container.update(provider=PROVIDER, model=MODEL, turns=2)
        container["usage"] = {key: 0 for key in container["usage"]}
        container["tools"].update(
            total=2,
            succeeded=2,
            failed=0,
            uncertain=0,
            unexpectedFailureCount=0,
            workspaceWriteSucceeded=1,
            submitResultSucceeded=1,
            succeededToolNames=["apply_dashboard_spec", "submit_result"],
        )
    build = {key: execution[key] for key in ("frameworkVersion", "buildRevision", "gitRevision")}
    suite = {"id": "test-suite", "schemaVersion": 2, "productControlCaseIds": [result["id"]]}
    evidence = {
        "schemaVersion": 1,
        "suiteId": suite["id"],
        "suiteSchemaVersion": 2,
        **build,
        "caseIds": [result["id"]],
        "passed": True,
        "startedAt": execution["startedAt"],
        "finishedAt": execution["completedAt"],
        "results": [result],
    }
    return evidence, suite, build


def test_deterministic_product_control_is_separate_from_paid_model_attestation():
    evidence, suite, build = valid()
    assert attest_product_controls(evidence, suite, build) == {"passed": True, "problems": []}


@pytest.mark.parametrize(
    "tamper", ["usage", "lineage", "receipt", "tools", "build", "duplicates", "validation"]
)
def test_modified_control_evidence_is_rejected(tamper):
    evidence, suite, build = valid()
    result = evidence["results"][0]
    execution = result["agentExecution"]
    if tamper == "usage":
        execution["usage"]["inputTokens"] = 1
    elif tamper == "lineage":
        execution["runs"][0]["requestId"] = "unrelated"
    elif tamper == "receipt":
        result["missionAcceptance"]["acceptedReceiptId"] = "other-receipt"
    elif tamper == "tools":
        execution["tools"]["workspaceWriteSucceeded"] = 0
    elif tamper == "build":
        execution["gitRevision"] = None
    elif tamper == "duplicates":
        evidence["results"].append(copy.deepcopy(result))
    else:
        result["validation"]["checks"][0]["status"] = "failed"
    checked = attest_product_controls(evidence, suite, build)
    assert checked["passed"] is False and checked["problems"]
