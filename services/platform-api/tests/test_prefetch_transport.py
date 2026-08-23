import asyncio

import httpx
import pytest

from alphadeck_platform.prefetch_transport import (
    MarketDataError,
    MarketTransport,
    WorkspaceArtifacts,
    collect_datasets,
)


def test_market_transport_rejects_successful_http_with_wrong_symbol_or_history_window():
    async def run():
        calls, warnings = [], []

        async def upstream(request):
            calls.append(request)
            symbol = "600036" if "wrong" in request.url.params else "600519"
            date = "2025-07-01" if "future" in request.url.params else "2025-06-01"
            return httpx.Response(
                200,
                json={
                    "symbol": symbol,
                    "period": "daily",
                    "adjustment": "none",
                    "data_quality": {"status": "warning"},
                    "bars": [{"date": date, "close": 100}],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            transport = MarketTransport(client=client, base_url="http://fixture:8100")
            assert (
                await transport.fetch(
                    "/api/v1/quotes/history/600519", on_quality_warning=warnings.append
                )
            )["symbol"] == "600519"
            assert len(warnings) == 1
            with pytest.raises(MarketDataError, match="symbol_mismatch"):
                await transport.fetch("/api/v1/quotes/history/600519?wrong=1")
            temporal = {
                "startDate": "2025-06-01",
                "endDate": "2025-06-30",
                "asOf": "2025-06-30T15:59:59.999Z",
            }
            with pytest.raises(MarketDataError, match="outside_research_window"):
                await transport.fetch(
                    "/api/v1/quotes/history/600519?future=1", research_time=temporal
                )
            assert calls[-1].url.params["end"] == "20250630"
            for path in ("http://other/api/v1/data", "/api/v1/../admin", "/api/v1/%2e%2e/admin"):
                with pytest.raises(MarketDataError):
                    await transport.fetch(path)

    asyncio.run(run())


def test_platform_artifacts_are_written_only_under_generation_authority(tmp_path):
    async def run():
        active = False

        async def authority(operation):
            if not active:
                raise RuntimeError("generation lease lost")
            return await operation(None)

        artifacts = WorkspaceArtifacts(tmp_path, authority)
        with pytest.raises(RuntimeError, match="lease lost"):
            await artifacts.write("data_file/raw/request/test.json", {"value": 1})
        assert artifacts.read("data_file/raw/request/test.json") is None
        active = True
        assert (
            await artifacts.write("data_file/raw/request/test.json", {"value": 1.0})
            == "data_file/raw/request/test.json"
        )
        assert artifacts.read("data_file/raw/request/test.json") == {"value": 1}
        assert (tmp_path / "data_file/raw/request/test.json").read_text() == '{\n  "value": 1\n}\n'
        with pytest.raises(ValueError):
            await artifacts.write("app/page.json", {})
        (tmp_path / "evidence").symlink_to(tmp_path / "data_file", target_is_directory=True)
        with pytest.raises(ValueError, match="links"):
            await artifacts.write("evidence/sources.json", {})

    asyncio.run(run())


def test_symbol_prefetch_is_bounded_ordered_and_progress_is_serialized():
    async def run():
        active, maximum, counts = 0, 0, []
        reporting = False

        async def fetch(symbol, quote, raw, warnings):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02 if symbol == "600519" else 0.001)
            active -= 1
            if symbol == "000001":
                raise RuntimeError("fixture upstream failed")
            raw.append("data_file/raw/" + symbol + ".json")
            return {"symbol": symbol}

        async def progress(value):
            nonlocal reporting
            assert not reporting
            reporting = True
            await asyncio.sleep(0.001)
            counts.append(value)
            reporting = False

        result = await collect_datasets(
            ["600519", "600036", "000001", "300750"], fetch, on_progress=progress
        )
        assert maximum == 2
        assert [asset["symbol"] for asset in result["assets"]] == ["600519", "600036", "300750"]
        assert [value["completed"] for value in counts] == [1, 2, 3, 4]
        assert counts[-1] == {"completed": 4, "total": 4, "succeeded": 3, "failed": 1}
        assert len(result["warnings"]) == 1 and "000001" in result["warnings"][0]

    asyncio.run(run())


def test_cancelled_prefetch_waits_for_all_inflight_cleanup_before_return():
    async def run():
        started, cleaned = asyncio.Event(), []

        async def fetch(symbol, *_args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.append(symbol)

        task = asyncio.create_task(collect_datasets(["600519", "600036"], fetch))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert set(cleaned) == {"600519", "600036"}

    asyncio.run(run())
