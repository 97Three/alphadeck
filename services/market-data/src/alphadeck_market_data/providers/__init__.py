"""市场数据源适配器。"""

from alphadeck_market_data.providers.base import (
    HistoricalKlineProvider,
    MarketDataProvider,
    ProviderCapability,
)

__all__ = [
    "HistoricalKlineProvider",
    "MarketDataProvider",
    "ProviderCapability",
]
