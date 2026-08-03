import math

from .finance_values import bars_from_asset, coalesce, js_string, mean, numeric, record, rounded
from .messages import js_slice


def window_return(closes, window):
    latest = closes[-1] if closes else None
    baseline = closes[-1 - window] if len(closes) > window else closes[0] if closes else None
    if not latest or not baseline or baseline <= 0:
        return None
    return rounded(((latest - baseline) / baseline) * 100)


def calculate_metrics(kline):
    bars = (
        [item for item in kline["bars"] if isinstance(item, dict)]
        if isinstance(kline, dict) and isinstance(kline.get("bars"), list)
        else []
    )
    fields = {
        key: [numeric(bar.get(key)) for bar in bars if numeric(bar.get(key)) is not None]
        for key in ("close", "volume", "amount", "high", "low")
    }
    closes = fields["close"]
    first, last = closes[0] if closes else None, closes[-1] if closes else None
    returns = [
        (close - closes[index]) / closes[index] if closes[index] else 0
        for index, close in enumerate(closes[1:])
    ]
    average = mean(returns) or 0
    variance = (mean([(value - average) ** 2 for value in returns]) or 0) if returns else 0
    peak, drawdown = first or 0, 0
    for close in closes:
        peak = max(peak, close)
        if peak > 0:
            drawdown = min(drawdown, ((close - peak) / peak) * 100)
    return {
        "periodReturn": rounded(((last - first) / first) * 100) if first and last else None,
        "return20d": window_return(closes, 20),
        "return60d": window_return(closes, 60),
        "return120d": window_return(closes, 120),
        "periodHigh": rounded(max(fields["high"])) if fields["high"] else None,
        "periodLow": rounded(min(fields["low"])) if fields["low"] else None,
        "maxDrawdown": rounded(drawdown),
        "volatility20d": rounded(math.sqrt(variance) * math.sqrt(252) * 100),
        "avgVolume20d": rounded(mean(fields["volume"][-20:])),
        "avgAmount20d": rounded(mean(fields["amount"][-20:])),
        **{"ma" + str(window): rounded(mean(closes[-window:])) for window in (5, 10, 20, 60)},
    }


def bar_date(bars, *, last=False):
    value = (bars[-1] if last else bars[0]) if bars else {}
    date = coalesce(value.get("date"), value.get("time"), value.get("trade_date"))
    return js_slice(date, 0, 10) if isinstance(date, str) else None


def ensure_technical_summary(asset):
    quote = record(asset.get("quote")) or {}
    kline = coalesce(record(asset.get("kline")), record(asset.get("history")))
    bars = bars_from_asset(asset)
    metrics = record(asset.get("computedMetrics"))
    metrics = calculate_metrics(kline) if metrics is None else metrics
    indicators = record(asset.get("technicalIndicators")) or {}
    existing = record(indicators.get("summary")) or {}

    def pick(*keys):
        return coalesce(
            *(numeric(existing.get(key)) for key in keys[:-1]), numeric(metrics.get(keys[-1]))
        )

    latest = coalesce(
        numeric(existing.get("latest_close")),
        numeric(quote.get("price")),
        numeric((bars[-1] if bars else {}).get("close")),
    )
    averages = {key: pick(key, key) for key in ("ma5", "ma10", "ma20", "ma60")}
    return20, return60 = pick("return_20d_pct", "return20d"), pick("return_60d_pct", "return60d")
    return120 = coalesce(
        numeric(existing.get("return_120d_pct")),
        numeric(existing.get("period_return_pct")),
        numeric(metrics.get("return120d")),
        numeric(metrics.get("periodReturn")),
    )
    drawdown = pick("max_drawdown_pct", "maxDrawdown")
    volatility = pick("volatility_20d_annualized_pct", "volatility_annualized_pct", "volatility20d")
    volume = pick("avg_volume20", "avgVolume20d")
    symbol = js_string(coalesce(asset.get("symbol"), quote.get("symbol"), ""))
    name = js_string(coalesce(asset.get("name"), quote.get("name"), symbol))
    available = [value for value in averages.values() if value is not None]
    trend = "趋势待确认：K 线或均线样本不足。"
    if latest is not None and len(available) >= 3:
        label = "MA5/MA10/MA20" if averages["ma60"] is None else "MA5/MA10/MA20/MA60"
        if all(latest >= value for value in available):
            trend = f"趋势偏强：最新价站上 {label}。"
        elif all(latest < value for value in available):
            trend = f"趋势偏弱：最新价低于 {label}。"
        else:
            trend = "震荡观察：最新价处于均线区间内，需要等待方向确认。"
    summary = {
        "symbol": symbol,
        "name": name,
        "sample_window": f"{bar_date(bars) or '-'} 至 {bar_date(bars, last=True) or '-'}，{len(bars)} 根日 K"
        if bars
        else "暂无历史 K 线样本",
        "return_20d_pct": rounded(return20),
        "return_60d_pct": rounded(return60),
        "return_120d_pct": rounded(return120),
        "period_return_pct": rounded(pick("period_return_pct", "periodReturn")),
        "max_drawdown_pct": rounded(drawdown),
        "volatility_20d_annualized_pct": rounded(volatility),
        **{key: rounded(value) for key, value in averages.items()},
        "latest_close": rounded(latest),
        "trend_state": js_string(coalesce(existing.get("trend_state"), trend)),
        "volume_note": "缺少连续成交量，量能指标待补充。"
        if volume is None
        else f"20 日均量约 {js_string(rounded(volume, 0))} 手。",
    }
    asset["technicalIndicators"] = {
        **indicators,
        "symbol": symbol,
        "summary": summary,
        "computedMetrics": metrics,
        "data_quality": coalesce(
            indicators.get("data_quality"),
            {
                "status": "ok" if len(bars) >= 20 else "warning",
                "warnings": []
                if len(bars) >= 20
                else ["历史 K 线样本少于 20 条，技术指标稳定性较弱。"],
            },
        ),
    }
    return summary
