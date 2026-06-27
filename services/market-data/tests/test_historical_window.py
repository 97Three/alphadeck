import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from alphadeck_market_data.cache import MarketDataCache
from alphadeck_market_data.contracts.quotes import KlineBar, KlineResponse
from alphadeck_market_data.services import backtests, indicators, quotes
from alphadeck_market_data.services.kline_window import constrain_kline_window, validate_window


def response() -> KlineResponse:
    return KlineResponse(
        symbol="600519",
        secid="1.600519",
        period="daily",
        adjustment="none",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        bars=[
            KlineBar(
                date=(date(2025, 1, 1) + timedelta(days=i)).isoformat(), close=Decimal(100 + i)
            )
            for i in range(120)
        ],
    )


@pytest.mark.parametrize("local_ready", [True, False])
@pytest.mark.parametrize("operation", ["quotes", "technical", "ma", "strategy"])
def test_service_trims_before_calculation_and_keeps_cache_windows_separate(
    monkeypatch, tmp_path, local_ready, operation
):
    module = (
        quotes if operation == "quotes" else indicators if operation == "technical" else backtests
    )
    original = response()

    async def local(**kwargs):
        return original if local_ready else None

    async def provider(*args, **kwargs):
        return original  # Deliberately ignores requested dates.

    monkeypatch.setattr(module, "get_local_kline_if_ready", local)
    monkeypatch.setattr(module, "get_kline_local_first", provider)
    cache = MarketDataCache(enabled=True, root=tmp_path)

    def run(start):
        kwargs = dict(
            symbol="600519",
            period="daily",
            adjustment="none",
            limit=120,
            start=start,
            end="20250410",
            ttl_seconds=300,
        )
        if operation == "quotes":
            return asyncio.run(
                quotes.get_history_quote(None, cache, None, **kwargs, refresh=False)
            ).bars
        if operation == "technical":
            return asyncio.run(indicators.get_technical_indicators(None, cache, **kwargs)).points
        kwargs.update(initial_cash=Decimal(100000), fee_bps=Decimal(5))
        if operation == "ma":
            result = asyncio.run(
                backtests.get_ma_crossover_backtest(
                    None, cache, **kwargs, fast_window=5, slow_window=20
                )
            )
        else:
            result = asyncio.run(
                backtests.get_strategy_backtest(
                    None,
                    cache,
                    **kwargs,
                    strategy_id="ma_crossover",
                    parameters={"fast_window": "5", "slow_window": "20"},
                )
            )
        assert result.experiment is not None
        return result.equity_curve

    assert run("20250110")[0].date == "2025-01-10"
    bounded = run("20250111")
    assert bounded[0].date == "2025-01-11"
    assert bounded[-1].date == "2025-04-10"
    assert original.bars[0].date == "2025-01-01"  # Shared/cache responses are not mutated.


@pytest.mark.parametrize(
    "start,end,period",
    [
        ("20250229", "20250301", "daily"),
        ("20250302", "20250301", "daily"),
        ("20250101", "20250301", "minute5"),
    ],
)
def test_invalid_window_is_rejected(start, end, period):
    with pytest.raises(ValueError):
        validate_window(period, start, end)


def test_empty_historical_window_never_returns_latest_bars():
    with pytest.raises(ValueError, match="No daily bars"):
        constrain_kline_window(response(), start="20240101", end="20240131")
