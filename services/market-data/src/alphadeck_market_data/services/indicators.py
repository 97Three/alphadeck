from __future__ import annotations

from alphadeck_market_data.cache import MarketDataCache
from alphadeck_market_data.contracts.quotes import (
    Adjustment,
    KlinePeriod,
    TechnicalIndicatorsResponse,
)
from alphadeck_market_data.indicators import build_technical_indicators
from alphadeck_market_data.providers.base import HistoricalKlineProvider
from alphadeck_market_data.services.caching import cache_response, read_cached_response
from alphadeck_market_data.services.kline_gateway import (
    get_kline_local_first,
    get_local_kline_if_ready,
)
from alphadeck_market_data.services.kline_window import constrain_kline_window, validate_window


async def get_technical_indicators(
    client: HistoricalKlineProvider,
    cache: MarketDataCache,
    *,
    symbol: str,
    period: KlinePeriod,
    adjustment: Adjustment,
    limit: int,
    end: str,
    ttl_seconds: int,
    start: str | None = None,
) -> TechnicalIndicatorsResponse:
    validate_window(period, start, end)
    normalized_limit = max(1, min(limit, 1000))
    local = await get_local_kline_if_ready(
        symbol=symbol,
        period=period,
        adjustment=adjustment,
        limit=normalized_limit,
        end=end,
    )
    if local is not None:
        local = constrain_kline_window(local, start=start, end=end)
        return build_technical_indicators(local)
    cache_key = cache.build_key(
        "technical-indicators",
        {
            "symbol": symbol,
            "period": period,
            "adjustment": adjustment,
            "limit": normalized_limit,
            "end": end,
            "start": start,
            "window_version": 1,
        },
    )
    cached = read_cached_response(cache, cache_key, TechnicalIndicatorsResponse)
    if cached is not None:
        return cached

    kline = await get_kline_local_first(
        client,
        symbol=symbol,
        period=period,
        adjustment=adjustment,
        limit=normalized_limit,
        end=end,
        bypass_local=True,
    )
    kline = constrain_kline_window(kline, start=start, end=end)
    response = build_technical_indicators(kline)
    return cache_response(cache, cache_key, ttl_seconds, response, TechnicalIndicatorsResponse)
