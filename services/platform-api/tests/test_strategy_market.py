import asyncio
import json
from pathlib import Path

import httpx
import pytest

from alphadeck_platform import strategy_mappers
from alphadeck_platform.strategy_api import permission
from alphadeck_platform.strategy_market import StrategyMarket

CASES = json.loads((Path(__file__).parent / "fixtures/strategy-market.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_native_market_adapter_matches_original_calls_and_projections(case, monkeypatch):
    monkeypatch.setenv("ALPHADECK_MARKET_ADMIN_TOKEN", "synthetic-market-admin")
    monkeypatch.setenv("ALPHADECK_MARKET_API_ENABLED", "1")
    monkeypatch.setattr(strategy_mappers, "now", lambda: "2026-11-03T00:00:00.000Z")
    calls = []

    def handler(request):
        calls.append(
            {
                "path": request.url.path,
                "query": dict(request.url.params),
                "method": request.method,
                "body": json.loads(request.content) if request.content else None,
                "admin": request.headers.get("x-alphadeck-admin-token"),
            }
        )
        return httpx.Response(200, json=case["payload"])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            value = await StrategyMarket(client, base_url="http://market.test").action(
                case["action"], case["body"]
            )
        assert value == case["expected"]
        assert calls == case["calls"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "action,path",
    [
        ("technical-indicators", "/api/v1/indicators/technical/600519"),
        ("financial-reports", "/api/v1/fundamentals/financials/600519"),
        ("fundamental-indicators", "/api/v1/indicators/fundamental/600519"),
        ("announcements", "/api/v1/events/announcements/600519"),
        ("trading-calendar", "/api/v1/foundation/trading-calendar"),
        ("resolve-symbol", "/api/v1/symbols/resolve"),
    ],
)
def test_new_market_explorer_actions_are_read_only(action, path, monkeypatch):
    monkeypatch.setenv("ALPHADECK_MARKET_API_ENABLED", "1")
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("x-alphadeck-admin-token")))
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await StrategyMarket(client, base_url="http://market.test").action(
                action, {"symbol": "600519", "query": "600519"}
            )

    asyncio.run(run())
    assert permission(action) == "quant.data.read"
    assert calls == [("GET", path, None)]


def test_eastmoney_batch_uses_requested_universe_slice_across_page_boundary():
    market = StrategyMarket(base_url="http://market.test")
    pages = []
    writes = []

    async def members(universe, *, page, page_size):
        pages.append((universe, page, page_size))
        start = (page - 1) * 100
        return {
            "total": 102,
            "members": [{"symbol": f"{index:06d}.SH"} for index in range(start, min(start + 100, 102))],
        }

    async def request(path, *, method="GET", body=None, admin=False, timeout=30):
        writes.append((path, method, body, admin))
        return {"completed_symbols": 3, "failed_symbols": 0, "rows_upserted": 10}

    market.members = members
    market.request = request
    result = asyncio.run(market.action("run-ingestion-batch", {
        "universeId": "fixture", "provider": "eastmoney", "offset": 99, "batchSize": 3,
    }))

    assert pages == [("fixture", 1, 100), ("fixture", 2, 100)]
    assert len(writes) == 1
    assert writes[0][0:2] == ("/api/v1/ingestion/eastmoney/history", "POST")
    assert writes[0][2]["symbols"] == ["000099.SH", "000100.SH", "000101.SH"]
    assert writes[0][3] is True
    assert result["next_offset"] == 102
