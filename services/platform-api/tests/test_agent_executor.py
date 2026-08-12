import asyncio
import json
from pathlib import Path

import pytest
from test_run_engine import engine_context  # noqa: F401

from alphadeck_platform.agent_executor import AgentExecutor, execution_contract
from alphadeck_platform.projects import Projects

FIXTURES = Path(__file__).parent / "fixtures"


def test_application_executor_compiles_skills_creates_real_run_and_returns_candidate(
    engine_context,  # noqa: F811
    monkeypatch,
):
    database, input, _, temporary = engine_context
    root = temporary / input["projectId"]
    root.mkdir()
    case = next(
        value
        for value in json.loads((FIXTURES / "agent-prompts.json").read_text())["assessments"]
        if value["name"] == "singleStockData_valid:valid"
    )
    for relative, key in (
        (".data-agent/finance-run-plan.json", "plan"),
        ("data_file/final/dashboard-data.json", "final"),
        ("evidence/sources.json", "sources"),
        ("evidence/data_quality.json", "quality"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(case[key]))
    # The deterministic lane must work even when no model credential is configured.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MODELPORT_API_KEY", raising=False)

    async def run():
        async with database.transaction() as tx:
            await tx.execute(
                "UPDATE projects SET repo_path=%s WHERE id=%s", (str(root), input["projectId"])
            )
        projects = Projects(database, temporary)
        executor = AgentExecutor(
            projects,
            provider_factory=lambda _: pytest.fail("Prepared standard lane contacted a model"),
        )
        candidate = await executor.execute(
            input["projectId"],
            "分析600519，生成标准看板",
            input["requestId"],
            model="deepseek-v4-flash",
        )
        assert candidate["source"] == "pi_agent_submit_result"
        assert candidate["sourceRequestId"] == input["requestId"]
        assert candidate["declaredArtifacts"] == ["src/App.vue", "src/style.css"]
        row = await database.fetch_one(
            "SELECT * FROM agent_runs WHERE id=%s", (candidate["sourceRunId"],)
        )
        assert row["status"] == "candidate_complete" and row["total_tokens"] == 0
        assert row["skill_hash"].startswith("sha256:")
        assert (root / "src/App.vue").is_file()
        # Only the independent verifier can accept a Mission.
        assert (
            await database.fetch_one(
                "SELECT id FROM agent_missions WHERE project_id=%s AND status='completed'",
                (input["projectId"],),
            )
            is None
        )

    asyncio.run(run())


def test_prepared_custom_lane_has_only_hash_guarded_edits(tmp_path):
    case = next(
        value
        for value in json.loads((FIXTURES / "agent-prompts.json").read_text())["assessments"]
        if value["name"] == "singleStockData_valid:valid"
    )
    for relative, key in (
        (".data-agent/finance-run-plan.json", "plan"),
        ("data_file/final/dashboard-data.json", "final"),
        ("evidence/sources.json", "sources"),
        ("evidence/data_quality.json", "quality"),
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(case[key]))
    selected = execution_contract(tmp_path, "把看板配色改成蓝色")
    assert selected["graph"]["lane"] == "model_custom"
    options = selected["toolOptions"]
    assert options["include_semantic_edit"] and not options["include_dashboard_spec"]
    assert options["profile_allowed_write_globs"] == ["src/App.vue", "src/style.css"]
    assert not options["include_default"] and not options["include_quant_api"]
