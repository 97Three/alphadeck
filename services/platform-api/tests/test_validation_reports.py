import json
import os

from alphadeck_platform.validation_reports import assess_freshness, read_report


def test_report_freshness_tracks_run_and_key_artifacts_without_mutating_history(tmp_path):
    control = tmp_path / ".data-agent"
    control.mkdir()
    (tmp_path / "src").mkdir()
    page = tmp_path / "src/App.vue"
    page.write_text("original page")
    state = control / "generation-state.json"
    state.write_text(json.dumps({"requestId": "request-1"}))
    report = control / "validation.json"
    original = {
        "schemaVersion": 1,
        "runId": "request-1",
        "status": "passed",
        "passed": True,
        "checks": [{"id": "build", "status": "passed"}],
    }
    report.write_text(json.dumps(original))
    os.utime(page, (1000, 1000))
    os.utime(report, (1001, 1001))
    assert read_report(tmp_path) == original
    os.utime(page, (1002, 1002))
    stale = read_report(tmp_path)
    check = stale["checks"][-1]
    assert check["id"] == "validation_report_stale"
    assert check["metadata"]["reasons"] == ["artifact_modified_after_report"]
    assert check["metadata"]["staleArtifactPaths"] == ["src/App.vue"]
    state.write_text(json.dumps({"requestId": "request-2"}))
    assert read_report(tmp_path)["checks"][-1]["metadata"]["reasons"] == [
        "artifact_modified_after_report",
        "run_id_mismatch",
    ]
    assert json.loads(report.read_text()) == original
    result = assess_freshness(
        " request-1 ",
        "request-2",
        100,
        [{"path": "a", "mtimeMs": 99}, {"path": "b", "mtimeMs": 101}],
    )
    assert result["staleArtifactPaths"] == ["b"] and result["newestArtifactMtimeMs"] == 101
