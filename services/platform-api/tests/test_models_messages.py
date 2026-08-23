import asyncio
import json
from uuid import uuid4

import httpx
import psycopg

from alphadeck_platform.auth import Authorization
from alphadeck_platform.database import Database
from alphadeck_platform.messages import (
    compact_output,
    insert_message,
    js_length,
    serialize_message,
)
from alphadeck_platform.models import availability
from app import create_app


def test_models_share_provider_reads_and_never_expose_upstream_secrets(monkeypatch):
    monkeypatch.setenv("MODELPORT_API_KEY", "modelport-fixture-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "official-fixture-secret")
    seen = []

    def respond(request):
        seen.append(request)
        if request.url.host == "api.deepseek.com":
            return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})
        return httpx.Response(403, json={"sensitive": "must-not-leak"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await availability(client)
        assert len(seen) == 2
        assert result["models"] == ["deepseek-v4-flash"]
        assert [item["status"] for item in result["modelChecks"]] == [
            "unauthorized",
            "unauthorized",
            "available",
        ]
        assert "secret" not in json.dumps(result) and "must-not-leak" not in json.dumps(result)

    asyncio.run(run())


def test_models_detect_disabled_missing_and_malformed_catalogue(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("MODELPORT_API_KEY", "fixture")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{}]}))
        ) as client:
            result = await availability(client)
            assert [item["status"] for item in result["modelChecks"]] == [
                "invalid_response",
                "invalid_response",
                "unconfigured",
            ]
            monkeypatch.setenv("ALPHADECK_DEGRADATION_MODE", "offline")
            result = await availability(client)
            assert [item["status"] for item in result["modelChecks"]] == [
                "disabled",
                "disabled",
                "unconfigured",
            ]

    asyncio.run(run())


def test_message_history_cursor_ties_conversation_delete_and_tool_compaction(
    database_url, actor, tmp_path
):
    identity = "messages-" + str(uuid4())
    database = Database(database_url)
    app = create_app(
        database, Authorization(database, enabled=True, secret="test-secret"), tmp_path
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO projects (id,name,owner_id,updated_at) VALUES (%s,'Message contract',%s,now())",
            (identity, actor["user_id"]),
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"alphadeck.session_token": actor["cookie"]},
        ) as client:
            path = f"/api/chat/{identity}/messages"
            bad = await client.post(path, json={"content": "Hello", "projectId": "other"})
            assert bad.status_code == 400
            for number in range(3):
                result = await client.post(
                    path,
                    json={
                        "content": f"  Message {number}  ",
                        "conversationId": "a" if number < 2 else "b",
                    },
                )
                assert result.status_code == 201, result.text
                assert result.json()["data"]["content"] == f"Message {number}"
            with psycopg.connect(database_url) as connection:
                connection.execute(
                    "UPDATE messages SET created_at='2026-01-01T00:00:00Z' WHERE project_id=%s",
                    (identity,),
                )
            first = (await client.get(path, params={"limit": 1})).json()
            assert first["totalCount"] == 3 and first["pagination"]["hasMore"]
            cursor = first["data"][0]
            following = (
                await client.get(
                    path, params={"after": cursor["createdAt"], "afterId": cursor["id"], "limit": 2}
                )
            ).json()
            assert len(following["data"]) == 2
            assert cursor["id"] not in [row["id"] for row in following["data"]]
            assert following["pagination"]["mode"] == "incremental"
            assert (await client.get(path, params={"after": "invalid"})).status_code == 400
            removed = await client.delete(path, params={"conversationId": "a"})
            assert removed.json()["deleted"] == 2
            async with database.transaction() as connection:
                row = await insert_message(
                    connection,
                    identity,
                    "tool",
                    "start\n" + "诊断😀" * 6000 + "\nTAIL",
                    message_type="tool_result",
                    metadata={"output": "x" * 20000},
                )
            serial = serialize_message(row)
            assert js_length(serial["content"]) <= 12000
            assert serial["content"].endswith("TAIL")
            assert serial["metadata"]["toolOutputOriginalChars"] == js_length(row["content"])
            response = await client.get(path)
            assert response.status_code == 200
            assert response.json()["data"][-1]["metadata"]["toolOutputTruncated"]
        assert compact_output("abc", 0) == ""

    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute("DELETE FROM projects WHERE id=%s", (identity,))
