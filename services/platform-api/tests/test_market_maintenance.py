import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from alphadeck_platform.market_maintenance import estimated_date, evaluate_freshness, maintain


def test_market_freshness_uses_completed_session_and_authoritative_holiday_calendar():
    assert (
        estimated_date(datetime(2026, 11, 2, 17, tzinfo=ZoneInfo("Asia/Shanghai"))) == "2026-10-30"
    )
    assert (
        estimated_date(datetime(2026, 11, 2, 18, tzinfo=ZoneInfo("Asia/Shanghai"))) == "2026-11-02"
    )
    checked = evaluate_freshness(
        dict(
            calendarThrough="2026-10-08",
            latestOpenDate="2026-09-30",
            latestBarDate="2026-09-30",
            symbolsAtLatest=100,
        ),
        estimated="2026-10-08",
        min_symbols=100,
    )
    assert checked["ok"] and checked["expectedBarDate"] == "2026-09-30"
    assert not evaluate_freshness(
        dict(calendarThrough=None, latestOpenDate=None, latestBarDate=None, symbolsAtLatest=0),
        estimated="2026-11-03",
    )["ok"]


@pytest.mark.parametrize("total", [0, 301, True, None])
def test_market_maintenance_rejects_unbounded_scope_before_writing(total):
    async def run():
        calls = []

        def upstream(request):
            calls.append(request.method)
            return httpx.Response(200, json={"total": total})

        async with httpx.AsyncClient(
            base_url="http://market", transport=httpx.MockTransport(upstream)
        ) as client:
            with pytest.raises(ValueError, match="bounded plan"):
                await maintain(
                    SimpleNamespace(calendar_only=False, dry_run=False, skip_freshness=True),
                    client=client,
                    today=date(2026, 11, 3),
                )
        assert calls == ["GET"]

    asyncio.run(run())


def test_market_maintenance_dry_run_reads_scope_without_starting_any_jobs():
    async def run():
        calls = []

        def upstream(request):
            calls.append(request.method)
            return httpx.Response(200, json={"total": 100})

        async with httpx.AsyncClient(
            base_url="http://market", transport=httpx.MockTransport(upstream)
        ) as client:
            result = await maintain(
                SimpleNamespace(calendar_only=False, dry_run=True, skip_freshness=True),
                client=client,
                today=date(2026, 11, 3),
            )
        assert result["scope"]["totalSymbols"] == 100 and result["historyBody"]["max_batches"] == 12
        assert calls == ["GET"]

    asyncio.run(run())


def test_market_maintenance_observes_completed_job_and_rejects_partial_results():
    async def run(status):
        writes = []

        def upstream(request):
            if "/members" in request.url.path:
                return httpx.Response(200, json={"total": 100})
            if request.method == "POST":
                writes.append(request.url.path)
                return httpx.Response(200, json={"job_id": "owned-job"})
            jobs = (
                []
                if not any("autofill" in path for path in writes)
                else [
                    {
                        "id": "owned-job",
                        "status": status,
                        "completed_symbols": 100,
                        "failed_symbols": 0,
                    }
                ]
            )
            return httpx.Response(200, json={"jobs": jobs})

        async with httpx.AsyncClient(
            base_url="http://market", transport=httpx.MockTransport(upstream)
        ) as client:
            return await maintain(
                SimpleNamespace(calendar_only=False, dry_run=False, skip_freshness=True),
                client=client,
                today=date(2026, 11, 3),
            )

    assert asyncio.run(run("completed"))["ingestion"]["completed_symbols"] == 100
    with pytest.raises(ValueError, match="bounded scope"):
        asyncio.run(run("partial"))
