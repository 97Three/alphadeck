import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alphadeck_platform.auth import verify_cookie
from alphadeck_platform.mission import compile_mission
from alphadeck_platform.permissions import evaluate_permission
from alphadeck_platform.progress import evaluate_turn
from alphadeck_platform.settings import normalize_model
from alphadeck_platform.workspace import project_path, workspace_file, write_atomic

FIXTURES = Path(__file__).parent / "fixtures"


def test_progress_replay_matches_typescript_when_work_repeats_or_regresses():
    cases = json.loads((FIXTURES / "progress.json").read_text(encoding="utf-8"))
    for case in cases:
        assert evaluate_turn(case["previous"], case["observation"]) == case["expected"]


def test_model_aliases_match_typescript():
    cases = json.loads((FIXTURES / "models.json").read_text(encoding="utf-8"))
    for case in cases:
        assert normalize_model(case["input"]) == case["expected"]


def test_project_llm_config_matches_original_environment_rules(monkeypatch):
    from alphadeck_platform.llm_config import project_llm_config

    keys = [
        "ALPHADECK_LLM_AGENT_ENABLED",
        "ALPHADECK_LLM_QUERY_REWRITE_ENABLED",
        "ALPHADECK_QUERY_REWRITE_LLM_TIMEOUT_MS",
        "ALPHADECK_QUERY_REWRITE_LLM_MAX_RETRIES",
    ]
    cases = json.loads((FIXTURES / "llm-config.json").read_text())
    for case in cases:
        for key in keys:
            monkeypatch.delenv(key, raising=False)
        for key, value in case["environment"].items():
            monkeypatch.setenv(key, value)
        assert project_llm_config(case["input"]) == case["expected"]


def test_migration_audit_requires_every_route_and_native_runtime_component():
    from alphadeck_platform.audit import report
    from app import create_app

    status = report(create_app())
    assert status["routeFiles"] == 91
    assert status["implementedMethodContracts"] == 117
    assert status["implementedMethodContracts"] + status["pendingMethodContracts"] == 117
    assert status["pendingWorkers"] == []
    assert status["sourceDrift"] == []
    assert status["routingProblems"] == []
    assert status["partiallyImplementedPaths"] == []
    assert "/api/projects" in status["forwardedPaths"]
    assert {
        "path": "/api/chat/[project_id]/act",
        "method": "POST",
        "source": "src/app/api/chat/[project_id]/act/route.ts",
    } not in status["missing"]
    assert status["missing"] == []
    assert status["pendingRuntimeComponents"] == []
    assert status["complete"] is True


def test_all_permission_decisions_match_original_typescript():
    cases = json.loads((FIXTURES / "permissions.json").read_text(encoding="utf-8"))
    assert len(cases) == 1296
    for case in cases:
        assert evaluate_permission(case["input"]) == case["expected"], case["input"]


def test_mission_contract_matches_original_typescript():
    case = json.loads((FIXTURES / "mission.json").read_text(encoding="utf-8"))
    assert compile_mission(case["input"]) == case["expected"]


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a/../../x", "a\x00b"])
def test_mission_rejects_escaping_artifacts(path):
    case = json.loads((FIXTURES / "mission.json").read_text(encoding="utf-8"))
    data = deepcopy(case["input"])
    data["definition"]["artifacts"][0]["path"] = path
    with pytest.raises(ValueError):
        compile_mission(data)


def test_invalid_expiration_cannot_turn_a_deny_into_an_allow():
    data = {
        "action": "project.create",
        "actor": {"id": "member"},
        "policy": {
            "userOverrides": [
                {"permissionKey": "project.create", "effect": "deny", "expiresAt": "invalid"}
            ]
        },
    }
    assert evaluate_permission(data)["reason"] == "EXPLICIT_DENY"
    data["policy"]["userOverrides"][0]["expiresAt"] = "2020-01-01T00:00:00Z"
    assert evaluate_permission(data, datetime(2026, 1, 1, tzinfo=UTC))["allowed"]


def test_workspace_symlink_escape_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        workspace_file(workspace, "link/secret")
    with pytest.raises(ValueError):
        project_path(workspace, "../outside")
    (workspace / "real-project").mkdir()
    (workspace / "alias-project").symlink_to(workspace / "real-project", target_is_directory=True)
    with pytest.raises(ValueError):
        project_path(workspace, "alias-project")
    write_atomic(workspace, "data/result.json", b"{}")
    assert (workspace / "data/result.json").read_bytes() == b"{}"


def test_cookie_signature_is_compatible_and_tampering_fails():
    # SHA-256 HMAC for the same value/secret and base64 format used by better-call.
    import base64
    import hmac

    signature = base64.b64encode(hmac.digest(b"test-secret", b"session-token", "sha256")).decode()
    cookie = "session-token." + signature
    assert verify_cookie(cookie, "test-secret") == "session-token"
    assert verify_cookie(cookie, "wrong-secret") is None
    assert verify_cookie("changed." + signature, "test-secret") is None
    assert verify_cookie("unsigned-session", "test-secret") is None
