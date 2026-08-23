import asyncio
import json
from copy import deepcopy
from pathlib import Path

import httpx

from alphadeck_platform.api_idempotency import ApiOperations
from alphadeck_platform.auth import Authorization
from alphadeck_platform.database import Database
from app import create_app

FIXTURE = json.loads((Path(__file__).parent / "fixtures/query-rewrite.json").read_text())


def test_query_rewrite_route_blocks_parallel_reexecution_and_replays_result(database_url, actor):
    async def run():
        database, entered, release = Database(database_url), asyncio.Event(), asyncio.Event()
        calls = []

        async def rewriter(query, options):
            calls.append((query, options))
            entered.set()
            await release.wait()
            return deepcopy(FIXTURE["cases"][0]["expected"])

        app = create_app(
            database,
            Authorization(database, enabled=True, secret="test-secret"),
            query_rewriter=rewriter,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"alphadeck.session_token": actor["cookie"]},
            headers={"Idempotency-Key": "query-contract-" + actor["user_id"]},
        ) as client:
            body = {"query": "分析贵州茅台趋势", "model": "deepseek-v4-flash", "purpose": "preview"}
            first = asyncio.create_task(client.post("/api/quant/query/rewrite", json=body))
            await asyncio.wait_for(entered.wait(), 3)
            blocked = await client.post("/api/quant/query/rewrite", json=body)
            assert (
                blocked.status_code == 409
                and blocked.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
            )
            release.set()
            response = await first
            assert response.status_code == 200, response.text
            replay = await client.post("/api/quant/query/rewrite", json=body)
            assert replay.status_code == 200 and replay.headers["idempotency-replayed"] == "true"
            assert replay.json() == response.json() and len(calls) == 1
            conflict = await client.post(
                "/api/quant/query/rewrite", json={**body, "query": "different query"}
            )
            assert (
                conflict.status_code == 409
                and conflict.json()["error"]["code"] == "IDEMPOTENCY_PAYLOAD_CONFLICT"
            )
            rows = await database.fetch_all(
                "SELECT metric,quantity FROM usage_events WHERE actor_user_id=%s ORDER BY metric",
                (actor["user_id"],),
            )
            assert rows == [
                {"metric": "llm.total_tokens.monthly", "quantity": 15},
                {"metric": "query_rewrite.llm.daily", "quantity": 1},
            ]
        async with database.transaction() as tx:
            await tx.execute(
                "DELETE FROM api_idempotency_operations WHERE actor_key=%s", (actor["user_id"],)
            )

    asyncio.run(run())


def test_query_rewrite_response_commit_failure_keeps_retry_fence(database_url, actor, monkeypatch):
    async def fail_commit(*_args, **_kwargs):
        raise OSError("fixture result store unavailable")

    monkeypatch.setattr(ApiOperations, "complete", fail_commit)

    async def run():
        database, calls = Database(database_url), []

        async def rewriter(*_args):
            calls.append(True)
            return deepcopy(FIXTURE["cases"][0]["expected"])

        app = create_app(
            database,
            Authorization(database, enabled=True, secret="test-secret"),
            query_rewriter=rewriter,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            cookies={"alphadeck.session_token": actor["cookie"]},
            headers={"Idempotency-Key": "failed-contract-" + actor["user_id"]},
        ) as client:
            response = await client.post(
                "/api/quant/query/rewrite", json={"query": "分析贵州茅台趋势"}
            )
            assert (
                response.status_code == 503
                and response.json()["error"]["code"] == "IDEMPOTENCY_COMMIT_FAILED"
            )
            retry = await client.post(
                "/api/quant/query/rewrite", json={"query": "分析贵州茅台趋势"}
            )
            assert retry.status_code == 409 and len(calls) == 1
        async with database.transaction() as tx:
            await tx.execute(
                "DELETE FROM api_idempotency_operations WHERE actor_key=%s", (actor["user_id"],)
            )

    asyncio.run(run())


def test_symbol_resolution_normalizes_query_and_records_quota_once(database_url, actor):
    async def run():
        calls = []

        async def upstream(request):
            calls.append(request)
            return httpx.Response(200, json={"results": [{"symbol": "600519", "name": "贵州茅台"}]})

        database = Database(database_url)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as market:
            app = create_app(
                database,
                Authorization(database, enabled=True, secret="test-secret"),
                market_client=market,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                cookies={"alphadeck.session_token": actor["cookie"]},
            ) as client:
                response = await client.get(
                    "/api/quant/symbols/resolve", params={"query": " ６００５１９ ", "count": "99"}
                )
                assert response.status_code == 200 and response.json()["success"]
                assert (
                    calls[0].url.params["query"] == "600519" and calls[0].url.params["count"] == "5"
                )
                assert (await client.get("/api/quant/symbols/resolve?query=x")).status_code == 400
        assert len(calls) == 1
        usage = await database.fetch_one(
            "SELECT sum(quantity) AS n FROM usage_events WHERE actor_user_id=%s AND metric='quant.data_units.daily'",
            (actor["user_id"],),
        )
        assert usage["n"] == 1

    asyncio.run(run())
