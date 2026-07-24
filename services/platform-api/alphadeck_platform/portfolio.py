import math

from .finance_values import (
    bars_from_asset,
    coalesce,
    js_string,
    left_sum,
    mean,
    numeric,
    record,
    rounded,
)


def return_series(asset):
    ordered = []
    for index, bar in enumerate(bars_from_asset(asset)):
        date = js_string(coalesce(bar.get("date"), bar.get("time"), bar.get("trade_date"), index))[
            :10
        ]
        close = numeric(bar.get("close"))
        if close is not None and close > 0:
            ordered.append((date, close))
    return {
        current[0]: math.log(current[1] / previous[1])
        for previous, current in zip(ordered, ordered[1:], strict=False)
        if previous[1] > 0
    }


def pearson(left, right):
    if len(left) < 3 or len(left) != len(right):
        return None
    left_mean, right_mean = mean(left), mean(right)
    numerator = left_variance = right_variance = 0
    for first, second in zip(left, right, strict=True):
        a, b = first - left_mean, second - right_mean
        numerator += a * b
        left_variance += a**2
        right_variance += b**2
    denominator = math.sqrt(left_variance * right_variance)
    return None if denominator == 0 else numerator / denominator


def correlation_summary(assets):
    series, lengths = {}, {}
    for asset in assets:
        symbol = js_string(
            coalesce(asset.get("symbol"), (record(asset.get("quote")) or {}).get("symbol"), "")
        )
        returns = return_series(asset) if symbol else {}
        if returns:
            series[symbol], lengths[symbol] = returns, len(returns)
    symbols, matrix, pairs = list(series), [], []
    for left in symbols:
        row = {"symbol": left}
        for right in symbols:
            common = [date for date in series[left] if date in series[right]]
            correlation = pearson(
                [series[left][date] for date in common], [series[right][date] for date in common]
            )
            row[right] = rounded(correlation, 4)
            if left < right:
                pairs.append(
                    {
                        "left": left,
                        "right": right,
                        "correlation": rounded(correlation, 4),
                        "overlap": len(common),
                    }
                )
        matrix.append(row)
    pairs.sort(key=lambda pair: abs(coalesce(numeric(pair["correlation"]), -1)), reverse=True)
    return {
        "method": "pearson_log_return",
        "symbols": symbols,
        "sample_lengths": lengths,
        "matrix": matrix,
        "top_pairs": pairs[:10],
        "data_quality": {
            "status": "ok" if len(symbols) >= 2 else "warning",
            "warnings": [] if len(symbols) >= 2 else ["相关性计算至少需要两个有历史 K 线的标的。"],
        },
    }


def liquidity_summary(assets):
    rows = []
    for asset in assets:
        quote, bars = record(asset.get("quote")) or {}, bars_from_asset(asset)
        volumes = [
            numeric(bar.get("volume"))
            for bar in bars[-20:]
            if numeric(bar.get("volume")) is not None
        ]
        amounts = [
            numeric(bar.get("amount"))
            for bar in bars[-20:]
            if numeric(bar.get("amount")) is not None
        ]
        latest = coalesce(numeric(quote.get("amount")), amounts[-1] if amounts else None)
        average_amount, average_volume = mean(amounts), mean(volumes)
        market_cap = coalesce(
            numeric(quote.get("float_market_cap")), numeric(quote.get("market_cap"))
        )
        turnover = (
            (coalesce(latest, average_amount, 0) / market_cap) * 100
            if market_cap and coalesce(latest, average_amount)
            else None
        )
        previous, values = None, []
        for bar in bars[-60:]:
            close, amount = numeric(bar.get("close")), numeric(bar.get("amount"))
            if (
                close is not None
                and previous is not None
                and previous > 0
                and amount is not None
                and amount > 0
            ):
                values.append(abs(close / previous - 1) / amount)
            if close is not None:
                previous = close
        amihud = mean(values)
        warnings = []
        if len(bars) < 20:
            warnings.append("K 线样本少于 20 条，流动性均值稳定性较弱。")
        if average_amount is None:
            warnings.append("缺少成交额字段，无法计算 20 日平均成交额。")
        if amihud is None:
            warnings.append("缺少连续收盘价或成交额，无法计算 Amihud 非流动性。")
        rows.append(
            {
                "symbol": js_string(coalesce(asset.get("symbol"), quote.get("symbol"), "")),
                "name": js_string(
                    coalesce(asset.get("name"), quote.get("name"), asset.get("symbol"), "")
                ),
                "sample_size": len(bars),
                "latest_amount": rounded(latest),
                "avg_amount_20d": rounded(average_amount),
                "avg_volume_20d": rounded(average_volume),
                "turnover_proxy_pct": rounded(turnover, 4),
                "amihud_illiquidity_x1e9": rounded(
                    None if amihud is None else amihud * 1000000000, 6
                ),
                "liquidity_score": "unknown"
                if average_amount is None
                else "high"
                if average_amount >= 1000000000
                else "medium"
                if average_amount >= 100000000
                else "low",
                "warnings": warnings,
            }
        )
    warnings = [warning for row in rows for warning in row["warnings"]]
    return {
        "method": "amount_volume_amihud_proxy",
        "window": "20d",
        "rows": rows,
        "data_quality": {"status": "warning" if warnings else "ok", "warnings": warnings},
    }


def holding_rows(assets):
    weight, rows = 1 / len(assets) if assets else 0, []
    for asset in assets:
        quote = record(asset.get("quote")) or {}
        symbol, price = (
            js_string(coalesce(asset.get("symbol"), quote.get("symbol"), "")),
            numeric(quote.get("price")),
        )
        rows.append(
            {
                "symbol": symbol,
                "name": js_string(coalesce(asset.get("name"), quote.get("name"), symbol)),
                "shares": None,
                "cost_price": None,
                "current_price": price,
                "market_value": None if price is None else rounded(price * weight * 10000),
                "weight": rounded(weight * 100, 2),
                "pnl": None,
                "pnl_pct": None,
                "source": "market_prefetch",
                "data_gaps": ["shares", "cost_price", "actual_market_value"],
            }
        )
    return rows


def portfolio_summary(assets):
    holdings = holding_rows(assets)
    weights = [
        numeric(holding["weight"]) for holding in holdings if numeric(holding["weight"]) is not None
    ]
    return {
        "generated_from": "symbols_without_broker_position_detail",
        "total_asset": None,
        "market_value": None,
        "cash": None,
        "position_pct": None,
        "floating_pnl": None,
        "floating_pnl_pct": None,
        "concentration": {
            "max_weight_pct": max(weights) if weights else None,
            "top3_weight_pct": rounded(left_sum(sorted(weights, reverse=True)[:3])),
            "method": "equal_weight_proxy_until_user_position_fields_are_confirmed",
        },
        "data_gaps": ["total_asset", "cash", "shares", "cost_price", "actual_weight"],
        "warnings": [
            "平台只能从用户问题和行情接口预取标的，真实持仓数量、成本、现金和权重需要用户补充或由截图识别后确认。"
        ],
    }
