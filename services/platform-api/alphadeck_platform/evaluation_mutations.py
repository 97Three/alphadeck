import argparse
import json
import time
from copy import deepcopy

from .agent_reviewer import parse_review
from .config import ROOT
from .data_quality import assess_response
from .evaluation_diagnostics import trace_diagnostics
from .evaluation_evaluators import apply_evaluator, is_current
from .evaluation_oracles import evaluate
from .evaluation_queue import date
from .evaluation_snapshots import attest_snapshot, payload_sha256
from .evidence_quality import assess_evidence
from .generation_state import now
from .skill_source import strict_json
from .workspace import write_atomic


def evaluate_fixture(fixture):
    oracle = evaluate(fixture["assertions"], fixture["targets"])
    result = fixture["result"]
    result["artifacts"] = {**result.get("artifacts", {}), "oracle": oracle}
    trace = trace_diagnostics(result, fixture["mode"])
    result["traceDiagnostics"] = trace
    evaluation = apply_evaluator(fixture["evaluatorId"], fixture["mode"], result)
    if fixture.get("tamperScore"):
        evaluation["score"] = 100 if evaluation["score"] == 0 else 0
    data = assess_response(
        "/api/v1/quotes/history/600519",
        fixture["targets"]["finalData"]["kline"],
        {"adjustment": "qfq", "period": "daily"},
    )
    evidence = assess_evidence(fixture["targets"]["sources"], fixture["targets"]["quality"])
    catalogue = {
        key: {
            "path": key + ".json",
            "sha256": "sha256:" + "a" * 64,
            "bytes": 100,
            "truncated": False,
            "value": {"symbol": "600519"},
        }
        for key in ("finalData", "sources", "quality", "runPlan")
    }
    semantic = parse_review(
        json.dumps(fixture["semantic"]),
        None,
        {"provider": "openai", "model": "mutation-fixture"},
        catalogue,
    )
    snapshot = attest_snapshot(
        fixture["snapshot"],
        fixture["snapshotPayload"],
        expected_case=fixture["snapshot"]["caseId"],
        now=date("2026-07-16T00:00:00.000Z"),
    )
    detected = [
        name
        for name, failed in (
            ("oracle", not oracle["passed"]),
            ("evaluator", not evaluation["passed"]),
            ("snapshot", not snapshot["passed"]),
            ("trace", bool(trace["primaryFailureStage"])),
            ("data", data["status"] == "failed"),
            ("evidence", not evidence["passed"]),
            ("semantic", semantic.get("evidenceValidation", {}).get("status") != "verified"),
            ("report", not is_current(evaluation)),
        )
        if failed
    ]
    return {
        "oracle": oracle,
        "evaluation": evaluation,
        "snapshot": snapshot,
        "trace": trace,
        "data": data,
        "evidence": evidence,
        "semantic": semantic,
        "detectedBy": detected,
    }


def run_mutations(evaluator="rule-strict", *, created_at=None):
    fixtures = strict_json((ROOT / "config/evaluation-mutation-fixtures.json").read_bytes())
    baseline_fixture = deepcopy(fixtures["baseline"])
    baseline_fixture["evaluatorId"] = evaluator
    baseline = evaluate_fixture(baseline_fixture)
    results = []
    for mutation in fixtures["mutations"]:
        fixture = deepcopy(mutation["fixture"])
        fixture["evaluatorId"] = evaluator
        evaluated = evaluate_fixture(fixture)
        results.append(
            {
                **{key: mutation[key] for key in ("id", "name", "category", "expectedDetector")},
                "killed": mutation["expectedDetector"] in evaluated["detectedBy"],
                "detectedBy": evaluated["detectedBy"],
                "problems": [
                    *evaluated["oracle"]["failures"],
                    *evaluated["snapshot"]["problems"],
                    *[
                        issue["code"] + "@" + issue["path"]
                        for issue in evaluated["data"]["issues"]
                        if issue["severity"] == "error"
                    ],
                    *evaluated["evidence"]["failures"],
                    *evaluated["semantic"].get("evidenceValidation", {}).get("issues", []),
                    *[
                        check["summary"]
                        for check in evaluated["evaluation"]["checks"]
                        if check["status"] == "failed"
                    ],
                ],
                "primaryFailureStage": evaluated["trace"]["primaryFailureStage"],
            }
        )
    killed = sum(item["killed"] for item in results)
    by_category = {}
    for category in dict.fromkeys(item["category"] for item in results):
        selected = [item for item in results if item["category"] == category]
        count = sum(item["killed"] for item in selected)
        by_category[category] = {
            "total": len(selected),
            "killed": count,
            "killRate": int(count / len(selected) * 100 + 0.5),
        }
    evaluation = baseline["evaluation"]
    return {
        "schemaVersion": 1,
        "createdAt": created_at or now(),
        "evaluator": {
            "id": evaluation["evaluatorId"],
            "version": evaluation["evaluatorVersion"],
            "rubricVersion": evaluation["rubricVersion"],
        },
        "baselinePassed": not baseline["detectedBy"],
        "total": len(results),
        "killed": killed,
        "survived": len(results) - killed,
        "killRate": int(killed / len(results) * 100 + 0.5) if results else 0,
        "byCategory": by_category,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-kill-rate", type=int, default=100)
    args = parser.parse_args()
    if not 0 <= args.min_kill_rate <= 100:
        parser.error("--min-kill-rate must be in 0..100")
    catalog = strict_json((ROOT / "benchmarks/alphadeck/mutation-catalog.json").read_bytes())
    report = run_mutations()
    expected = {
        item["id"]: (item["category"], item["expectedDetector"]) for item in catalog["mutations"]
    }
    actual = {
        item["id"]: (item["category"], item["expectedDetector"]) for item in report["results"]
    }
    report["catalog"] = {
        "id": catalog["id"],
        "version": catalog["version"],
        "sha256": payload_sha256(catalog),
    }
    path = (
        "tmp/alphadeck-eval-mutations/mutation-report-" + str(time.time_ns() // 1000000) + ".json"
    )
    write_atomic(ROOT, path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    print(
        json.dumps(
            {
                "baselinePassed": report["baselinePassed"],
                "killed": report["killed"],
                "total": report["total"],
                "killRate": report["killRate"],
                "report": path,
            },
            ensure_ascii=False,
        )
    )
    return (
        0
        if expected == actual
        and report["baselinePassed"]
        and not report["survived"]
        and report["killRate"] >= max(args.min_kill_rate, catalog.get("minimumKillRate", 0))
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
