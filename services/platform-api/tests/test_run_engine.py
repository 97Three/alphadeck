import asyncio
import json
from uuid import uuid4

import psycopg
import pytest

from alphadeck_platform.context_manager import ContextManager
from alphadeck_platform.database import Database
from alphadeck_platform.file_tools import FileTools
from alphadeck_platform.mutation_journal import list_journals
from alphadeck_platform.run_engine import RunEngine, operation_id
from alphadeck_platform.runtime_session import RuntimeSession
from alphadeck_platform.runtime_store import RuntimeStore
from alphadeck_platform.tool_approval import DurableApprovalHandler
from alphadeck_platform.tool_arguments import parse_arguments


class ScriptedProvider:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        turn = self.turns.pop(0)
        yield {
            "type": "response_start",
            "responseId": "response-" + str(len(self.requests)),
            "model": "fixture",
        }
        yield {"type": "reasoning_delta", "delta": "private reasoning must never be stored"}
        if isinstance(turn, str):
            yield {"type": "text_delta", "delta": turn}
            reason = "stop"
        else:
            for index, call in enumerate(turn):
                yield {
                    "type": "tool_call_delta",
                    "index": index,
                    "id": call["id"],
                    "nameDelta": call["name"],
                    "argumentsDelta": json.dumps(call["arguments"], ensure_ascii=False),
                }
            reason = "tool_calls"
        yield {
            "type": "usage",
            "usage": {
                "inputTokens": 10,
                "outputTokens": 3,
                "totalTokens": 13,
                "cachedInputTokens": 0,
                "cacheMissInputTokens": 10,
            },
        }
        yield {"type": "finish", "reason": reason, "rawReason": reason}


@pytest.mark.parametrize("decision", ["approve", "edit", "reject", "invalid_edit"])
def test_durable_approval_gates_physical_write_and_revalidates_edits(
    engine_context, database_url, actor, decision
):
    from alphadeck_platform.approvals import Approvals

    database, input, tools, workspace = engine_context
    write = next(item for item in tools.definitions if item["name"] == "write_file")
    write["approval"] = {
        "reason": "Review the proposed page",
        "allowedDecisions": ["approve", "edit", "reject"],
        "timeoutMs": 5000,
        "projectPublicInput": lambda value: value,
    }
    provider = ScriptedProvider(
        [
            [
                {
                    "id": "write-reviewed",
                    "name": "write_file",
                    "arguments": {"path": "src/App.vue", "content": "original proposal"},
                }
            ],
            [
                {
                    "id": "submit",
                    "name": "submit_result",
                    "arguments": {"summary": "Ready", "artifacts": ["src/App.vue"]},
                }
            ],
        ]
    )

    async def run():
        row = await RuntimeStore(database).create(input, owner="approval-worker")
        approvals = Approvals(database)

        async def handler(request, *, cancel=None):
            await approvals.create(request)
            assert not (workspace / "src/App.vue").exists()
            current = await database.fetch_one(
                "SELECT status FROM agent_runs WHERE id=%s", (input["id"],)
            )
            assert current["status"] == "waiting"
            assert (
                await database.fetch_one(
                    "SELECT id FROM agent_tool_executions WHERE run_id=%s", (input["id"],)
                )
                is None
            )
            edited = (
                {"path": "../escape.tsx", "content": "bad"}
                if decision == "invalid_edit"
                else {"path": "src/App.vue", "content": "approved edit"}
            )
            await approvals.resolve(
                input["projectId"],
                request["approvalId"],
                actor["user_id"],
                "edit" if "edit" in decision else decision,
                edited_input=edited if "edit" in decision else None,
                edited_supplied="edit" in decision,
            )
            return await DurableApprovalHandler(database, input["projectId"])(
                request, cancel=cancel
            )

        async with RuntimeSession(database, row) as session:
            result = await RunEngine(
                provider,
                "fixture",
                tools,
                approval_handler=handler,
                context_manager=ContextManager(20000, 2000, 18000),
            ).run([{"role": "user", "content": "Create a page"}], session=session)
        if decision in {"approve", "edit"}:
            assert result["status"] == "completed", result.get("error")
            assert (workspace / "src/App.vue").read_text() == (
                "approved edit" if decision == "edit" else "original proposal"
            )
        else:
            assert not (workspace / "src/App.vue").exists()
        events = await database.fetch_all(
            "SELECT event_type,payload FROM agent_events WHERE run_id=%s ORDER BY sequence",
            (input["id"],),
        )
        assert sum(event["event_type"] == "tool_approval_requested" for event in events) == 1
        assert sum(event["event_type"] == "tool_approval_resolved" for event in events) == 1
        assert all("projectPublicInput" not in json.dumps(request) for request in provider.requests)
        assert (
            await database.fetch_one("SELECT status FROM agent_runs WHERE id=%s", (input["id"],))
        )["status"] not in {"waiting", "running"}

    asyncio.run(run())


def test_agent_context_compaction_keeps_latest_cluster_and_receipts(engine_context):
    database, input, tools, workspace = engine_context
    (workspace / "data").mkdir()
    (workspace / "data/large.txt").write_text("行情资料 " * 10000)
    provider = ScriptedProvider(
        [
            [{"id": "read-large", "name": "read_file", "arguments": {"path": "data/large.txt"}}],
            [
                {
                    "id": "write",
                    "name": "write_file",
                    "arguments": {
                        "path": "src/App.vue",
                        "content": "export default function Page(){return 'OK'}",
                    },
                }
            ],
            [
                {
                    "id": "submit",
                    "name": "submit_result",
                    "arguments": {"summary": "Candidate", "artifacts": ["src/App.vue"]},
                }
            ],
        ]
    )

    async def run():
        row = await RuntimeStore(database).create(input, owner="context-worker")
        async with RuntimeSession(database, row) as session:
            result = await RunEngine(
                provider, "fixture", tools, context_manager=ContextManager(9000, 2000, 7000)
            ).run(
                [
                    {"role": "system", "content": "Keep the system prefix stable"},
                    {"role": "user", "content": "Create a page"},
                ],
                session=session,
            )
        assert result["status"] == "completed", result.get("error")
        assert len(provider.requests) == 3
        assert provider.requests[0]["messages"][0] == provider.requests[2]["messages"][0]
        assert any(
            "tool_result_truncation" in (message["content"] or "")
            for message in provider.requests[1]["messages"]
        )
        assert any(
            "Trusted Context Capsule" in (message["content"] or "")
            for message in provider.requests[2]["messages"]
        )
        assert not any(
            "Trusted Context Capsule" in (message["content"] or "")
            for message in result["messages"]
        )
        events = await database.fetch_all(
            "SELECT payload FROM agent_events WHERE run_id=%s AND event_type='context_compacted'",
            (input["id"],),
        )
        assert len(events) >= 2
        assert all(
            "行情资料" not in json.dumps(event, ensure_ascii=False)
            and "private reasoning" not in json.dumps(event)
            for event in events
        )

    asyncio.run(run())


@pytest.fixture
def engine_context(database_url, actor, tmp_path):
    project = "engine-" + str(uuid4())
    request = str(uuid4())
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO projects (id,name,owner_id,repo_path,updated_at) VALUES (%s,'Engine contract',%s,%s,now())",
            (project, actor["user_id"], str(tmp_path)),
        )
        connection.execute(
            "INSERT INTO user_requests (id,project_id,actor_user_id,instruction) VALUES (%s,%s,%s,'private prompt must never be stored in runtime tables')",
            (request, project, actor["user_id"]),
        )
    database = Database(database_url)
    input = {
        "id": "run-" + str(uuid4()),
        "projectId": project,
        "requestId": request,
        "workspaceKey": "sha256:" + uuid4().hex + uuid4().hex,
        "provider": "openai",
        "model": "fixture",
        "frameworkVersion": "python-1",
        "buildRevision": "migration",
        **{
            key: "a" * 64
            for key in ("profileHash", "promptHash", "toolHash", "skillHash", "workspaceHash")
        },
    }
    yield database, input, FileTools(tmp_path), tmp_path
    with psycopg.connect(database_url) as connection:
        connection.execute("DELETE FROM projects WHERE id=%s", (project,))


def test_agent_loop_runs_tools_with_real_fences_and_submits_only_a_candidate(
    database_url, actor, engine_context
):
    database, input, tools, workspace = engine_context
    provider = ScriptedProvider(
        [
            [
                {
                    "id": "write-1",
                    "name": "write_file",
                    "arguments": {
                        "path": "src/App.vue",
                        "content": "export default function Page(){return 'Ready'}",
                    },
                }
            ],
            [{"id": "read-1", "name": "read_file", "arguments": {"path": "src/App.vue"}}],
            [
                {
                    "id": "submit-1",
                    "name": "submit_result",
                    "arguments": {"summary": "Candidate ready", "artifacts": ["src/App.vue"]},
                }
            ],
        ]
    )

    async def run():
        row = await RuntimeStore(database).create(input, owner="engine-worker")
        async with RuntimeSession(database, row) as session:
            result = await RunEngine(provider, "fixture", tools, require_write=True).run(
                [
                    {
                        "role": "system",
                        "content": "private prompt must never be stored in runtime tables",
                    },
                    {"role": "user", "content": "Create a page"},
                ],
                session=session,
            )
        assert result["status"] == "completed", result.get("error")
        assert (
            result["turns"] == 3
            and result["terminalResult"]["data"]["candidateStatus"] == "candidate_complete"
        )
        assert len(provider.requests) == 3
        assert list_journals(workspace) == []
        assert (workspace / "src/App.vue").is_file()
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT status,total_tokens FROM agent_runs WHERE id=%s", (input["id"],)
            ).fetchone() == ("candidate_complete", 39)
            assert (
                connection.execute(
                    "SELECT status FROM user_requests WHERE id=%s", (input["requestId"],)
                ).fetchone()[0]
                == "pending"
            )
            assert connection.execute(
                "SELECT count(*),sum(quantity) FROM usage_events WHERE source_type='agent_run' AND source_id=%s",
                (input["id"],),
            ).fetchone() == (1, 39)
            for table, column in (
                ("agent_events", "payload"),
                ("agent_checkpoints", "public_state"),
                ("agent_tool_executions", "result_receipt"),
            ):
                records = connection.execute(
                    f"SELECT {column}::text FROM {table} WHERE run_id=%s", (input["id"],)
                ).fetchall()
                assert all(
                    "private reasoning" not in record[0] and "private prompt" not in record[0]
                    for record in records
                )
            executions = connection.execute(
                "SELECT status FROM agent_tool_executions WHERE run_id=%s", (input["id"],)
            ).fetchall()
            assert executions == [("succeeded",)] * 3

    asyncio.run(run())


def test_terminal_tool_cannot_share_turn_with_a_mutation(engine_context):
    _, _, tools, workspace = engine_context
    provider = ScriptedProvider(
        [
            [
                {
                    "id": "write",
                    "name": "write_file",
                    "arguments": {"path": "src/App.vue", "content": "bad"},
                },
                {"id": "submit", "name": "submit_result", "arguments": {"summary": "Too early"}},
            ]
        ]
    )
    result = asyncio.run(
        RunEngine(provider, "fixture", tools).run([{"role": "user", "content": "test"}])
    )
    assert result["status"] == "failed" and result["error"]["code"] == "TERMINAL_TOOL_NOT_EXCLUSIVE"
    assert not (workspace / "src/App.vue").exists()


def test_schema_rejection_never_prepares_or_writes_a_tool(database_url, engine_context):
    database, input, tools, workspace = engine_context
    provider = ScriptedProvider(
        [
            [
                {
                    "id": "write",
                    "name": "write_file",
                    "arguments": {"path": "src/App.vue", "content": "bad", "unexpected": True},
                }
            ],
            "Cannot proceed",
        ]
    )

    async def run():
        row = await RuntimeStore(database).create(input, owner="worker")
        async with RuntimeSession(database, row) as session:
            result = await RunEngine(provider, "fixture", tools).run(
                [{"role": "user", "content": "test"}], session=session
            )
        assert result["status"] == "stopped" and result["error"]["code"] == "TERMINAL_TOOL_REQUIRED"
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM agent_tool_executions WHERE run_id=%s", (input["id"],)
                ).fetchone()[0]
                == 0
            )
        assert not (workspace / "src/App.vue").exists()

    asyncio.run(run())


def test_cancellation_before_model_call_commits_a_cancelled_run(database_url, engine_context):
    database, input, tools, _ = engine_context
    provider = ScriptedProvider([])

    async def run():
        row = await RuntimeStore(database).create(input, owner="worker")
        cancel = asyncio.Event()
        cancel.set()
        async with RuntimeSession(database, row) as session:
            result = await RunEngine(provider, "fixture", tools).run(
                [{"role": "user", "content": "test"}], session=session, cancel=cancel
            )
        assert result["status"] == "cancelled" and not provider.requests
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM agent_runs WHERE id=%s", (input["id"],)
                ).fetchone()[0]
                == "cancelled"
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('```json\n{"x":1,}\n```', {"x": 1}),
        ('Here: {"text":"line\nbreak"}', {"text": "line\nbreak"}),
        ("", {}),
        ('{"text":"comma,}"}', {"text": "comma,}"}),
    ],
)
def test_tool_arguments_allow_only_bounded_syntax_repairs(raw, expected):
    assert parse_arguments(raw)["value"] == expected


def test_model_tool_id_is_hashed_with_framework_run_and_turn():
    call = {"id": "same-model-id", "name": "write_file"}
    first = operation_id("run-1", 1, call)
    assert first.startswith("op_") and len(first) == 67
    assert first != operation_id("run-2", 1, call)
    assert first != operation_id("run-1", 2, call)
