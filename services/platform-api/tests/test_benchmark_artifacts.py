import asyncio
import json
from pathlib import Path

from alphadeck_platform.benchmark_artifacts import event_audit, inspect_artifacts
from alphadeck_platform.benchmark_visual import check_metrics
from alphadeck_platform.scaffold import ensure_scaffold


def test_benchmark_inspection_uses_real_workspace_artifacts_and_rejects_tampering(tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures/data-prefetch.json").read_text())[0]
    for relative, value in fixture["expected"]["entries"].items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False))
    plan = tmp_path / ".data-agent/finance-run-plan.json"
    plan.parent.mkdir(exist_ok=True)
    plan.write_text(json.dumps(fixture["plan"]))
    ensure_scaffold(tmp_path, "controlled-benchmark", shared_modules=False)
    case = {"expectedSymbol": "600519", "expectedAssetType": "stock"}
    prefetch = fixture["expected"]["outcome"]
    first = asyncio.run(inspect_artifacts(tmp_path, case, prefetch))
    assert first["status"] == "passed", first["failures"]
    final = tmp_path / "data_file/final/dashboard-data.json"
    value = json.loads(final.read_text())
    value["symbol"] = "other"
    final.write_text(json.dumps(value))
    second = asyncio.run(inspect_artifacts(tmp_path, case, prefetch))
    assert second["status"] == "failed" and any(
        "symbol 应为" in failure for failure in second["failures"]
    )
    final.write_text('{"price": NaN}')
    third = asyncio.run(inspect_artifacts(tmp_path, case, prefetch))
    assert third["invalidArtifacts"] == ["data_file/final/dashboard-data.json"]


def test_missing_generation_events_never_count_as_a_complete_benchmark(tmp_path):
    report = event_audit(tmp_path, {})
    assert report["failures"] and report["total"] == 0


def test_visual_benchmark_retains_keyword_accessibility_and_layout_checks():
    metrics = {
        "text": "AlphaDeck 600519",
        "svgCount": 1,
        "canvasCount": 0,
        "rectCount": 0,
        "horizontalOverflow": False,
        "headingCount": 1,
        "accessibilityIssueCount": 0,
    }
    assert check_metrics(metrics, {"expectedVisualKeywords": ["600519"]}, "desktop") == []
    damaged = {
        **metrics,
        "text": "empty",
        "svgCount": 0,
        "horizontalOverflow": True,
        "headingCount": 3,
        "accessibilityIssueCount": 2,
    }
    assert len(check_metrics(damaged, {}, "mobile")) == 5
