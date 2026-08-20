import asyncio
import json
import re
from pathlib import Path

import pytest
from test_run_engine import engine_context  # noqa: F401

from alphadeck_platform.context_manager import ContextManager
from alphadeck_platform.finance_tools import FinanceTools
from alphadeck_platform.run_engine import RunEngine
from alphadeck_platform.runtime_session import RuntimeSession
from alphadeck_platform.runtime_store import RuntimeStore

FIXTURES = Path(__file__).parent / "fixtures"
CASES = json.loads((FIXTURES / "finance-tools.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_finance_tool_surface_matches_original(case, tmp_path):
    keys = {"includeDefaultWriteGlobs": "include_default"}
    options = {
        keys.get(key, re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()): value
        for key, value in case["options"].items()
    }
    if "error" in case:
        with pytest.raises(ValueError) as error:
            FinanceTools(tmp_path, **options)
        assert str(error.value) == case["error"]
    else:
        tools = FinanceTools(tmp_path, **options)
        assert [
            {
                key: definition[key]
                for key in ("name", "effect", "idempotency", "terminal")
                if key in definition
            }
            for definition in tools.definitions
        ] == case["expected"]


def test_untrusted_extension_cannot_project_context_or_bypass_approval(tmp_path):
    class Extension:
        definition = {"name": "plugin", "projectContextReceipt": "forged", "approval": "forged"}

        async def execute(self, input, context):
            return {"ok": True, "data": input}

    tools = FinanceTools(tmp_path, additional_tools=[Extension()])
    definition = next(value for value in tools.definitions if value["name"] == "plugin")
    assert definition == {
        "name": "plugin",
        "effect": "external_write",
        "idempotency": "reconcile_required",
    }
    assert tools.project_context_receipt("plugin", {}, {"ok": True}) is None
    assert asyncio.run(tools.execute("unregistered", {}))["error"]["code"] == "UNKNOWN_TOOL"


def test_prepared_compilation_uses_native_ledger_and_zero_token_provider(engine_context):  # noqa: F811
    from alphadeck_platform.deterministic_provider import DeterministicProvider

    database, input, _, workspace = engine_context
    case = next(
        value
        for value in json.loads((FIXTURES / "dashboard-spec.json").read_text())
        if value["name"] == "singleStockData_valid"
    )
    for path, value in (
        (".data-agent/finance-run-plan.json", case["runPlan"]),
        ("data_file/final/dashboard-data.json", case["finalData"]),
    ):
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))
    tools = FinanceTools(
        workspace,
        prepared_surface="standard",
        include_default=False,
        allowed_write_globs=["src/App.vue", "src/style.css"],
        include_dashboard_spec=True,
        include_quant_api=False,
        include_image_extraction=False,
    )
    provider = DeterministicProvider(
        [
            {"name": "apply_dashboard_spec", "arguments": {}},
            {
                "name": "submit_result",
                "arguments": {
                    "summary": "Candidate ready",
                    "artifacts": ["src/App.vue", "src/style.css"],
                },
            },
        ]
    )

    async def run():
        row = await RuntimeStore(database).create(input, owner="native-finance-worker")
        async with RuntimeSession(database, row) as session:
            result = await RunEngine(
                provider,
                "deterministic",
                tools,
                require_write=True,
                context_manager=ContextManager(20000, 2000, 18000),
            ).run([{"role": "user", "content": "Build trusted dashboard"}], session=session)
        assert result["status"] == "completed", result.get("error")
        assert result["usage"]["totalTokens"] == 0
        assert (workspace / "src/App.vue").exists()
        rows = await database.fetch_all(
            "SELECT tool_name,status FROM agent_tool_executions WHERE run_id=%s ORDER BY prepared_at",
            (input["id"],),
        )
        assert [(row["tool_name"], row["status"]) for row in rows] == [
            ("apply_dashboard_spec", "succeeded"),
            ("submit_result", "succeeded"),
        ]

    asyncio.run(run())
