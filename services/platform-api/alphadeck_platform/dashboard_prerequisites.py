from .finance_values import coalesce, left_sum, numeric, record
from .messages import js_length


def contract_string(value):
    return (
        value.strip()
        if isinstance(value, str) and value.strip() and js_length(value.strip()) <= 160
        else None
    )


def nested(value, key):
    return record((value or {}).get(key)) or {}


def records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def rows(value, key):
    return records(nested(value, key).get("rows"))


def symbols(values):
    return {
        code
        for value in values
        if (
            code := contract_string(
                coalesce(value.get("symbol"), nested(value, "quote").get("symbol"))
            )
        )
        is not None
    }


def has_numeric(value, keys):
    return any(numeric((value or {}).get(key)) is not None for key in keys)


def signal(data, computed_keys, summary_keys):
    for value, keys in (
        (nested(nested(data, "technicalIndicators"), "summary"), summary_keys),
        (nested(data, "computedMetrics"), computed_keys),
    ):
        for key in keys:
            parsed = numeric(value.get(key))
            if parsed is not None:
                return parsed
    return None


def satisfied(predicate, data):
    quote, bars = nested(data, "quote"), records(nested(data, "kline").get("bars"))
    comparison, assets = rows(data, "comparison"), records(data.get("assets"))
    if predicate == "hasSingleStockQuote":
        return (
            record(data.get("quote")) is not None
            and contract_string(coalesce(data.get("symbol"), quote.get("symbol"))) is not None
            and numeric(quote.get("price")) is not None
        )
    if predicate == "hasUsableKline":
        return (
            len(bars) >= 20
            and all(numeric(bar.get("close")) is not None for bar in bars[-20:])
            and numeric(bars[-1].get("volume")) is not None
        )
    if predicate in {"hasDerivedTrendSignals", "hasTechnicalMovingAverages"}:
        keys = (
            ("ma5", "ma20")
            if predicate == "hasDerivedTrendSignals"
            else ("ma5", "ma10", "ma20", "ma60")
        )
        return all((value := signal(data, [key], [key])) is not None and value > 0 for key in keys)
    if predicate == "hasDerivedVolumeSignals":
        latest = numeric((bars[-1] if bars else {}).get("volume"))
        average = signal(data, ["avgVolume20d"], ["avg_volume20", "avg_volume_20d"])
        return latest is not None and latest >= 0 and average is not None and average > 0
    if predicate == "hasDerivedRiskSignals":
        drawdown = signal(data, ["maxDrawdown"], ["max_drawdown_pct"])
        volatility = signal(
            data, ["volatility20d"], ["volatility_20d_annualized_pct", "volatility_annualized_pct"]
        )
        return (
            drawdown is not None
            and -100 <= drawdown <= 0
            and volatility is not None
            and volatility >= 0
        )
    if predicate == "hasComparisonCoverage":
        return (
            len(assets) >= 2
            and len(comparison) >= 2
            and len(symbols(assets)) == len(assets)
            and len(symbols(comparison)) == len(comparison)
            and symbols(assets) == symbols(comparison)
        )
    if predicate == "hasComparisonChartMetrics":
        return len(comparison) >= 2 and all(
            has_numeric(
                row, ["period_return", "period_return_pct", "return_120d_pct", "return_120d"]
            )
            and has_numeric(row, ["max_drawdown", "max_drawdown_pct"])
            and has_numeric(
                row, ["volatility20d", "volatility_20d_annualized_pct", "volatility20d_pct"]
            )
            for row in comparison
        )
    if predicate == "hasSelectionRanking":
        ranking = rows(data, "selectionRanking")
        ranks, scores = (
            [numeric(row.get("rank")) for row in ranking],
            [numeric(row.get("score")) for row in ranking],
        )
        return (
            len(ranking) >= 2
            and len(symbols(ranking)) == len(ranking)
            and symbols(ranking) == symbols(assets)
            and all(rank == index + 1 for index, rank in enumerate(ranks))
            and all(
                score is not None and (index == 0 or score <= scores[index - 1])
                for index, score in enumerate(scores)
            )
        )
    if predicate == "hasAssetSourceEvidence":
        return len(assets) >= 2 and all(
            contract_string(coalesce(asset.get("source"), nested(asset, "quote").get("source")))
            is not None
            for asset in assets
        )
    if predicate == "hasStrategyResearchContract":
        screener, conclusion = nested(data, "screener"), nested(data, "conclusion")
        return bool(
            contract_string(screener.get("mode"))
            and numeric(screener.get("scanned_symbols")) is not None
            and isinstance(screener.get("candidates"), list)
            and contract_string(conclusion.get("risk_disclaimer"))
        )
    if predicate == "hasPortfolioRiskCoverage":
        holdings = records(data.get("holdings"))
        weights = [
            numeric(coalesce(value.get("weight"), value.get("position_pct"))) for value in holdings
        ]
        return (
            len(holdings) >= 2
            and len(assets) == len(holdings)
            and len(symbols(holdings)) == len(holdings)
            and len(symbols(assets)) == len(assets)
            and symbols(holdings) == symbols(assets)
            and all(weight is not None and weight > 0 for weight in weights)
            and abs(left_sum(weights) - 100) <= 1
        )
    if predicate == "hasPortfolioRiskEvidence":
        portfolio, correlation = nested(data, "portfolio"), nested(data, "correlation")
        holdings = symbols(records(data.get("holdings")))
        correlated = {value for value in correlation.get("symbols", []) if contract_string(value)}
        liquid = rows(data, "liquidity")
        return (
            record(data.get("portfolio")) is not None
            and record(portfolio.get("concentration")) is not None
            and isinstance(portfolio.get("data_gaps"), list)
            and bool(portfolio["data_gaps"])
            and record(data.get("correlation")) is not None
            and isinstance(correlation.get("matrix"), list)
            and correlated == holdings
            and len(liquid) == len(holdings)
            and holdings.issubset(symbols(liquid))
        )
    if predicate in {"hasFundamentalReports", "hasFundamentalTrend"}:
        points = records(
            nested(
                data,
                "financials" if predicate == "hasFundamentalReports" else "fundamentalIndicators",
            ).get("reports" if predicate == "hasFundamentalReports" else "points")
        )
        return len(points) >= 2 and all(
            contract_string(point.get("report_date"))
            and numeric(point.get("revenue")) is not None
            and numeric(point.get("parent_net_profit")) is not None
            for point in points
        )
    if predicate == "hasFinancialQualityScore":
        quality = nested(data, "financialQuality")
        points = records(quality.get("rows"))
        if len(points) != 1:
            return False
        row, score = points[0], numeric(points[0].get("quality_score"))
        return bool(
            contract_string(row.get("symbol"))
            and contract_string(row.get("latest_report_date"))
            and score is not None
            and 0 <= score <= 100
            and contract_string(row.get("quality_label"))
            and all(isinstance(row.get(key), list) for key in ("strengths", "watch_items"))
            and isinstance(quality.get("limitations"), list)
        )
    if predicate == "hasAnnouncementContract":
        return isinstance(nested(data, "announcements").get("announcements"), list)
    if predicate == "hasCashFlowComparisonOrLimitation":
        comparison = nested(data, "fundamentalMetricComparison")
        return bool(
            contract_string(comparison.get("conclusion"))
            and contract_string(comparison.get("basis"))
        )
    backtest = nested(data, "backtest")
    if predicate == "hasReproducibleBacktest":
        return bool(
            contract_string(backtest.get("strategy_id"))
            and contract_string(backtest.get("strategy_name"))
            and all(
                numeric(backtest.get(key)) is not None
                for key in ("fast_window", "slow_window", "fee_bps")
            )
        )
    summary = nested(backtest, "summary")
    trade_count = numeric(summary.get("trade_count"))
    count_valid = trade_count is not None and trade_count >= 0 and trade_count == int(trade_count)
    if predicate == "hasBacktestSummary":
        return (
            count_valid
            and all(
                numeric(summary.get(key)) is not None
                for key in ("total_return_pct", "max_drawdown_pct")
            )
            and (trade_count == 0 or numeric(summary.get("win_rate_pct")) is not None)
        )
    if predicate == "hasBacktestSeriesAndTrades":
        if not isinstance(backtest.get("trades"), list):
            return False
        trades = records(backtest["trades"])
        points = records(backtest.get("equity_curve"))
        if (
            len([point for point in points if numeric(point.get("equity")) is not None]) < 2
            or len(trades) != len(backtest["trades"])
            or not count_valid
            or not all(contract_string(trade.get("entry_date")) for trade in trades)
        ):
            return False

        def completed(trade):
            status = (contract_string(trade.get("status")) or "").lower()
            return (
                status == "closed"
                or status in {"", "completed"}
                and contract_string(trade.get("exit_date")) is not None
            )

        closed = [trade for trade in trades if completed(trade)]
        return (
            all(
                contract_string(trade.get("exit_date"))
                and numeric(trade.get("return_pct")) is not None
                for trade in closed
            )
            and len(closed) == trade_count
            and (trade_count == 0 or bool(trades))
        )
    raise ValueError("Unknown trusted dashboard predicate: " + predicate)
