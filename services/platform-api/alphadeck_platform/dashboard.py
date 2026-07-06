import math

from .finance_values import bars_from_asset, coalesce, js_string, left_sum, numeric, record, rounded
from .fundamentals import financial_quality, rank_rows
from .technical import bar_date


def comparison_summary(assets):
    qualities = [
        record(asset.get("financialQuality"))
        if record(asset.get("financialQuality")) is not None
        else financial_quality(asset)
        for asset in assets
    ]
    rows = []
    for asset in assets:
        metrics, quote = (
            record(asset.get("computedMetrics")) or {},
            record(asset.get("quote")) or {},
        )
        symbol = js_string(coalesce(asset.get("symbol"), quote.get("symbol"), ""))
        bars = bars_from_asset(asset)
        technical = record((record(asset.get("technicalIndicators")) or {}).get("summary")) or {}
        quality = record(asset.get("financialQuality"))
        if quality is None:
            quality = next((row for row in qualities if row.get("symbol") == symbol), {})
        amount, average = numeric(quote.get("amount")), numeric(metrics.get("avgAmount20d"))
        row = {
            "symbol": symbol,
            "name": coalesce(asset.get("name"), quote.get("name"), symbol),
            "price": quote.get("price"),
            "change_percent": quote.get("change_percent"),
            "period_return": metrics.get("periodReturn"),
            "max_drawdown": metrics.get("maxDrawdown"),
            "volatility20d": metrics.get("volatility20d"),
            "avg_volume_20d": metrics.get("avgVolume20d"),
            "avg_amount_20d": average,
            "amount": quote.get("amount"),
            "as_of": coalesce(asset.get("as_of"), quote.get("quote_time"), quote.get("fetched_at")),
            "source": coalesce(asset.get("source"), quote.get("source")),
            "sample_size": len(bars),
            "period_start": bar_date(bars),
            "period_end": bar_date(bars, last=True),
            "return_20d_pct": coalesce(technical.get("return_20d_pct"), metrics.get("return20d")),
            "return_60d_pct": coalesce(technical.get("return_60d_pct"), metrics.get("return60d")),
            "return_120d_pct": coalesce(
                technical.get("return_120d_pct"),
                metrics.get("return120d"),
                metrics.get("periodReturn"),
            ),
            "financial_quality_score": quality.get("quality_score"),
            "financial_quality_label": quality.get("quality_label"),
            "liquidity_note": "20 日均成交额 " + js_string(rounded(average / 100000000)) + " 亿元"
            if average is not None
            else "最新成交额 " + js_string(rounded(amount / 100000000)) + " 亿元"
            if amount is not None
            else "成交额缺失，使用成交量代理。",
        }
        for key, value in {
            "periodReturnNumber": row["period_return"],
            "return120dNumber": coalesce(row["period_return"], row["return_120d_pct"]),
            "drawdownNumber": row["max_drawdown"],
            "volatilityNumber": row["volatility20d"],
            "liquidityNumber": coalesce(average, row["amount"]),
            "qualityNumber": row["financial_quality_score"],
        }.items():
            row[key] = numeric(value)
        rows.append(row)

    def leader(field, key, reverse=False):
        eligible = [row for row in rows if row[field] is not None]
        return sorted(eligible, key=key, reverse=reverse)[0] if eligible else None

    best = leader("return120dNumber", lambda row: row["return120dNumber"], True)
    drawdown = leader("drawdownNumber", lambda row: abs(row["drawdownNumber"]))
    volatility = leader("volatilityNumber", lambda row: row["volatilityNumber"])
    ranks = [
        rank_rows(rows, "return120dNumber", "desc"),
        rank_rows(
            [
                {**row, "drawdownAbs": abs(coalesce(row["drawdownNumber"], math.inf))}
                for row in rows
            ],
            "drawdownAbs",
            "asc",
        ),
        rank_rows(rows, "volatilityNumber", "asc"),
        rank_rows(rows, "liquidityNumber", "desc"),
        rank_rows(rows, "qualityNumber", "desc"),
    ]
    enriched = []
    for row in rows:
        values = [ranking.get(row["symbol"]) for ranking in ranks]
        valid = [value for value in values if value is not None]
        score = (
            rounded(
                left_sum([len(assets) - value + 1 for value in valid])
                / (len(valid) * len(assets))
                * 100,
                0,
            )
            if valid
            else None
        )
        reasons = [
            f"{label}排名 {value}"
            for label, value in (("收益", values[0]), ("回撤", values[1]), ("质量", values[4]))
            if value
        ]
        enriched.append(
            {
                **row,
                **dict(
                    zip(
                        (
                            "rank_return",
                            "rank_drawdown",
                            "rank_volatility",
                            "rank_liquidity",
                            "rank_quality",
                        ),
                        values,
                        strict=True,
                    )
                ),
                "composite_score": score,
                "relative_strength": "待确认"
                if row["return120dNumber"] is None
                else "收益领先"
                if row is best
                else "阶段为正"
                if row["return120dNumber"] > 0
                else "阶段偏弱",
                "selection_view": "数据待补齐"
                if score is None
                else "优先研究"
                if score >= 72
                else "观察研究"
                if score >= 55
                else "谨慎观察",
                "ranking_reason": "，".join(reasons) or "关键指标仍需补齐。",
                "exclusion_reason": "综合排名靠后，需等待趋势、质量或流动性改善。"
                if score is not None and score < 55
                else None,
            }
        )

    def project(row, field):
        return (
            {"symbol": row["symbol"], "name": row["name"], "value": field(row)}
            if row is not None
            else None
        )

    return {
        "rows": enriched,
        "leaders": {
            "best_return": project(
                best, lambda row: coalesce(row["period_return"], row["return_120d_pct"])
            ),
            "lowest_drawdown": project(drawdown, lambda row: row["max_drawdown"]),
            "lowest_volatility": project(volatility, lambda row: row["volatility20d"]),
        },
    }


def selection_ranking(comparison, assets):
    rows = [row for row in comparison.get("rows", []) if isinstance(row, dict)]
    rows = sorted(
        rows, key=lambda row: coalesce(numeric(row.get("composite_score")), -1), reverse=True
    )
    names = {
        "symbol": "symbol",
        "name": "name",
        "score": "composite_score",
        "view": "selection_view",
        "reason": "ranking_reason",
        "exclusion_reason": "exclusion_reason",
    }
    return {
        "method": "multi_factor_research_priority",
        "description": "综合收益、回撤、波动、流动性代理和财务质量后的研究优先级；不是交易指令。",
        "rows": [
            {"rank": index + 1, **{key: row[value] for key, value in names.items() if value in row}}
            for index, row in enumerate(rows)
        ],
        "coverage": {"requested": len(assets), "ranked": len(rows)},
    }


def trading_plan(assets, selection):
    ranks = {
        js_string(coalesce(row.get("symbol"), "")): numeric(row.get("rank"))
        for row in selection.get("rows", [])
        if isinstance(row, dict) and numeric(row.get("rank")) is not None
    }
    rows = []
    for asset in assets:
        quote, technical, metrics = (
            record(asset.get("quote")) or {},
            record((record(asset.get("technicalIndicators")) or {}).get("summary")) or {},
            record(asset.get("computedMetrics")) or {},
        )
        bars = bars_from_asset(asset)
        latest = record(bars[-1]) if bars else {}
        price = coalesce(
            numeric(quote.get("price")),
            numeric(technical.get("latest_close")),
            numeric(latest.get("close")),
        )
        if price is None or price <= 0:
            continue
        symbol = js_string(coalesce(asset.get("symbol"), quote.get("symbol"), ""))
        name = js_string(coalesce(asset.get("name"), quote.get("name"), symbol))
        averages = [numeric(technical.get(key)) for key in ("ma5", "ma10", "ma20")]
        ma5, ma10, _ = averages
        supports = sorted(
            [value for value in averages if value is not None and value > 0],
            key=lambda value: abs(value - price),
        )
        low = rounded(max(price * 0.94, (supports[0] if supports else price * 0.97) * 0.98))
        high = rounded(
            max(
                min(price * 1.01, max(coalesce(low, price) * 1.04, price * 0.995)),
                coalesce(low, price) * 1.01,
            )
        )
        latest_low = coalesce(numeric(quote.get("low")), numeric(latest.get("low")))
        stop = rounded(
            max(
                min(
                    coalesce(low, price) * 0.965,
                    latest_low * 0.98 if latest_low is not None else price * 0.94,
                    ma10 * 0.985 if ma10 is not None else price * 0.94,
                ),
                price * 0.86,
            )
        )
        risk = max(coalesce(high, price) - coalesce(stop, price * 0.94), price * 0.025)
        period_high = coalesce(
            numeric(metrics.get("periodHigh")),
            numeric(quote.get("high")),
            numeric(latest.get("high")),
            price,
        )
        first = rounded(
            max(
                coalesce(high, price) + risk * 1.5,
                price * 1.05,
                period_high * 1.01 if period_high is not None else price * 1.05,
            )
        )
        second = rounded(max(coalesce(first, price * 1.05) + risk, price * 1.10))
        ma_text = " / ".join(
            "MA" + str(window) + " " + js_string(rounded(value))
            for window, value in zip((5, 10, 20), averages, strict=True)
            if value is not None
        )
        abandon = [
            "高开超过 4% 且未回落到买入区间，不追。",
            f"盘中或收盘跌破 {js_string(stop)}，短线计划失效。"
            if stop is not None
            else "跌破买入区间下沿且不能收回，短线计划失效。",
        ]
        if ma5 is not None:
            abandon.append("收盘重新跌回 MA5 " + js_string(rounded(ma5)) + " 下方，降低优先级。")
        rows.append(
            {
                "rank": ranks.get(symbol),
                "symbol": symbol,
                "name": name,
                "current_price": rounded(price),
                "buy_zone_low": low,
                "buy_zone_high": high,
                "stop_loss": stop,
                "target_price_1": first,
                "target_price_2": second,
                "position_limit_pct": 30,
                "timeframe": "1-3 个交易日",
                "entry_style": "回踩承接，避免高开急拉追价",
                "abandon_condition": " ".join(abandon),
                "rationale": " ".join(
                    [
                        "当前价 "
                        + js_string(rounded(price))
                        + "，买入区间按最近均线支撑和当日波动回撤估算。",
                        "均线参考：" + ma_text + "。"
                        if ma_text
                        else "均线样本不足，优先控制仓位。",
                        "目标价以最近区间高点、风险收益比和短线波动共同约束。",
                    ]
                ),
                "risk_note": "这是基于本地行情和技术指标的短线交易计划，不构成收益承诺或即时交易指令。",
            }
        )
    return {
        "method": "ma_support_pullback_risk_plan",
        "description": "基于最新价、MA5/MA10/MA20、当日高低点和近期波动生成的短线买入/卖出风险计划。",
        "rows": sorted(rows, key=lambda row: coalesce(numeric(row.get("rank")), 999)),
        "limitations": [
            "仅使用本地行情、K 线和已计算指标，不读取用户真实账户、委托深度或盘中逐笔资金。",
            "若次日集合竞价、成交额、涨跌停状态或板块环境显著变化，需要重新刷新计划。",
            "单票仓位默认不超过 30%，实际执行还需结合总资金、持仓集中度和交易纪律。",
        ],
    }


def conclusion(comparison, selection, quality):
    leaders = record(comparison.get("leaders")) or {}
    top = record(selection["rows"][0]) if selection.get("rows") else None
    rows = [row for row in quality.get("rows", []) if isinstance(row, dict)]
    rows.sort(key=lambda row: coalesce(numeric(row.get("quality_score")), -1), reverse=True)

    def name(row):
        return js_string(coalesce(row.get("name"), row.get("symbol")))

    summary = [
        "综合研究优先级第一："
        + name(top)
        + "，原因："
        + js_string(coalesce(top.get("reason"), "多因子得分领先"))
        if top is not None
        else "综合研究优先级仍待补齐。",
        "阶段收益领先：" + name(leaders["best_return"]) + "。"
        if record(leaders.get("best_return")) is not None
        else "阶段收益领先标的待确认。",
        "回撤控制相对较好：" + name(leaders["lowest_drawdown"]) + "。"
        if record(leaders.get("lowest_drawdown")) is not None
        else "回撤控制指标待确认。",
        "财务质量相对占优：" + name(rows[0]) + "。" if rows else "财务质量数据仍需补齐。",
        "排序仅用于横向研究，不构成交易指令；仍需结合仓位、风险偏好、交易成本和后续基本面变化。",
    ]
    return {
        "summary": summary,
        "primary_view": name(top)
        + " 当前综合研究优先级最高；其余候选应结合趋势、质量和风险约束分层观察。"
        if top is not None
        else "候选排序待确认。",
        "risk_disclaimer": "公开行情和财务接口可能存在延迟或字段缺失，本结果不构成投资建议、收益承诺或即时交易指令。",
    }
