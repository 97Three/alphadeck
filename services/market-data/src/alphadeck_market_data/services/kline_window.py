from __future__ import annotations

from datetime import datetime

from alphadeck_market_data.contracts.quotes import KlineResponse


def parse_day(value: str) -> str:
    pattern = "%Y%m%d" if len(value) == 8 else "%Y-%m-%d"
    parsed = datetime.strptime(value, pattern).date()
    if parsed.strftime(pattern) != value:
        raise ValueError("Historical dates must use YYYYMMDD or YYYY-MM-DD.")
    return parsed.isoformat()


def validate_window(period: str, start: str | None, end: str) -> None:
    if start is None:
        return
    if period != "daily":
        raise ValueError("Explicit historical start is supported only for daily bars.")
    if parse_day(start) > parse_day(end):
        raise ValueError("Historical start must not be after end.")


def constrain_kline_window(
    response: KlineResponse, *, start: str | None, end: str
) -> KlineResponse:
    """Trim before calculation, even if the upstream provider ignores its cutoff.

    This bounds trading dates. It does not certify when a price revision was known.
    """
    validate_window(response.period, start, end)
    if response.period != "daily" or (start is None and end == "20500101"):
        return response
    upper = parse_day(end)
    lower = parse_day(start) if start else None
    bars = [
        bar
        for bar in response.bars
        if (lower is None or parse_day(bar.date) >= lower) and parse_day(bar.date) <= upper
    ]
    if not bars:
        raise ValueError("No daily bars are available within the requested historical window.")
    return response.model_copy(
        update={
            "bars": bars,
            "as_of": bars[-1].date,
            "metadata": {
                **response.metadata,
                "requested_window": {
                    "start": lower,
                    "end": upper,
                    "returned_bars": len(bars),
                    "availability_basis": "trading_dates_only",
                },
            },
        }
    )
