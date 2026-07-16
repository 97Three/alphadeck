import re

from .finance_values import coalesce, js_string, left_sum, numeric, record, rounded


def financial_quality(asset):
    quote = record(asset.get("quote")) or {}
    symbol = js_string(coalesce(asset.get("symbol"), quote.get("symbol"), ""))
    name = js_string(coalesce(asset.get("name"), quote.get("name"), symbol))
    fundamental, financials = (
        record(asset.get("fundamentalIndicators")) or {},
        record(asset.get("financials")) or {},
    )
    first = (
        record(financials["reports"][0])
        if isinstance(financials.get("reports"), list) and financials["reports"]
        else None
    )
    source = coalesce(record(fundamental.get("summary")), first, {})
    fields = [
        ("latest_weighted_roe", "weighted_roe"),
        ("latest_gross_margin", "gross_margin"),
        ("latest_net_margin", "net_margin"),
        ("latest_revenue_yoy", "revenue_yoy"),
        ("latest_net_profit_yoy", "net_profit_yoy"),
    ]
    roe, gross, net, revenue, profit = [
        numeric(coalesce(source.get(current), source.get(previous))) for current, previous in fields
    ]
    parts = [
        None if value is None else min(max(value * multiplier, low), high)
        for value, multiplier, low, high in (
            (roe, 3, 0, 30),
            (gross, 0.5, 0, 25),
            (net, 0.5, 0, 25),
            (revenue, 0.5, -10, 15),
            (profit, 0.25, -10, 15),
        )
    ]
    parts = [value for value in parts if value is not None]
    score = max(0, min(100, rounded(left_sum(parts), 0) or 0)) if parts else None
    label = (
        "财务质量待确认"
        if score is None
        else "盈利质量较强"
        if score >= 75
        else "质量与成长较均衡"
        if score >= 60
        else "质量约束较明显"
        if score >= 40
        else "财务质量偏弱或缺失较多"
    )
    strengths, watch = [], []
    if (gross or 0) >= 40:
        strengths.append("毛利率处于较高水平。")
    if (net or 0) >= 15:
        strengths.append("净利率表现较好。")
    if (revenue or 0) > 20 or (profit or 0) > 20:
        strengths.append("最近报告期仍有增长弹性。")
    if (roe or 0) < 5:
        watch.append("ROE 偏低，需要关注资产回报效率。")
    if (net or 0) < 8:
        watch.append("净利率偏低，盈利质量约束更强。")
    if (revenue or 0) < 0 or (profit or 0) < 0:
        watch.append("收入或利润同比为负，需要关注基本面压力。")
    quality = {
        "symbol": symbol,
        "name": name,
        "latest_report_date": coalesce(source.get("latest_report_date"), source.get("report_date")),
        "roe_pct": rounded(roe, 4),
        "gross_margin_pct": rounded(gross, 4),
        "net_margin_pct": rounded(net, 4),
        "revenue_yoy_pct": rounded(revenue, 4),
        "net_profit_yoy_pct": rounded(profit, 4),
        "quality_score": score,
        "quality_label": label,
        "strengths": strengths or ["可作为候选池横向比较样本。"],
        "watch_items": watch or ["仍需结合现金流、负债结构和行业景气度复核。"],
    }
    asset["financialQuality"] = quality
    return quality


def metric_comparison(financials, requested_time_range=None):
    reports = (
        [value for value in financials["reports"] if isinstance(value, dict)]
        if isinstance(financials.get("reports"), list)
        else []
    )
    if not reports:
        return None
    match = re.search(r"20[0-9]{2}", requested_time_range or "")
    year, annual = match[0] if match else None, "年报" in (requested_time_range or "")
    report = next(
        (
            value
            for value in reports
            if (not year or year in js_string(coalesce(value.get("data_type"), "")))
            and (not annual or "年报" in js_string(coalesce(value.get("data_type"), "")))
        ),
        reports[0],
    )
    date = js_string(coalesce(report.get("report_date"), ""))
    match = re.match(r"^[\s]*([+-]?[0-9]+)", date[:4])
    report_year = int(match[1]) if match else None
    previous = (
        next(
            (
                value
                for value in reports
                if js_string(coalesce(value.get("report_date"), ""))[:4] == str(report_year - 1)
                and js_string(coalesce(value.get("report_date"), ""))[4:10] == date[4:10]
            ),
            None,
        )
        if report_year is not None
        else None
    )
    current_raw, previous_raw = (
        record(report.get("raw")) or {},
        record((previous or {}).get("raw")) or {},
    )
    cash = numeric(
        coalesce(report.get("operating_cash_flow_per_share"), current_raw.get("MGJYXJJE"))
    )
    previous_cash = numeric(
        coalesce(
            (previous or {}).get("operating_cash_flow_per_share"), previous_raw.get("MGJYXJJE")
        )
    )
    cash_yoy = numeric(report.get("operating_cash_flow_per_share_yoy"))
    if cash_yoy is None and cash is not None and previous_cash is not None and previous_cash != 0:
        cash_yoy = ((cash - previous_cash) / abs(previous_cash)) * 100
    profit = numeric(report.get("net_profit_yoy"))
    outpaced = cash_yoy > profit if cash_yoy is not None and profit is not None else None
    return {
        "symbol": coalesce(report.get("symbol"), financials.get("symbol")),
        "reporting_period": coalesce(
            report.get("data_type"), report.get("report_date"), requested_time_range
        ),
        "operating_cash_flow_per_share": rounded(cash),
        "previous_operating_cash_flow_per_share": rounded(previous_cash),
        "operating_cash_flow_per_share_yoy": rounded(cash_yoy),
        "net_profit_yoy": rounded(profit),
        "cash_flow_outpaced_net_profit": outpaced,
        "conclusion": "现有财务摘要不足以完成经营现金流增速与净利润增速比较。"
        if outpaced is None
        else "每股经营现金流增速高于净利润增速。"
        if outpaced
        else "每股经营现金流增速未跑赢净利润增速。",
        "basis": "经营现金流使用每股经营活动现金流净额同比作为可核验代理口径。",
    }


def rank_rows(rows, field, direction):
    ranked = [
        (value["symbol"], numeric(value.get(field)))
        for value in rows
        if value["symbol"] and numeric(value.get(field)) is not None
    ]
    ranked.sort(key=lambda value: value[1], reverse=direction != "asc")
    return {symbol: index + 1 for index, (symbol, _) in enumerate(ranked)}


def financial_quality_summary(assets):
    rows = []
    for asset in assets:
        quality = record(asset.get("financialQuality"))
        rows.append(financial_quality(asset) if quality is None else quality)
    ranks = rank_rows(
        [{**row, "symbol": js_string(coalesce(row.get("symbol"), ""))} for row in rows],
        "quality_score",
        "desc",
    )
    return {
        "method": "latest_report_profitability_growth_score",
        "latest_report_date": next(
            (row["latest_report_date"] for row in rows if row.get("latest_report_date")), None
        ),
        "rows": [
            {**row, "rank_quality": ranks.get(js_string(coalesce(row.get("symbol"), "")))}
            for row in rows
        ],
        "limitations": [
            "财务质量评分用于横向研究，不构成估值目标价或买卖建议。",
            "当前仅使用接口可得的最近报告期、盈利率和同比指标，未纳入现金流、负债结构和行业景气度。",
        ],
    }
