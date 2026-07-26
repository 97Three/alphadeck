import math
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from zoneinfo import ZoneInfo

from .finance_values import coalesce, numeric
from .generation_state import now
from .strategy_mappers import array, record


def string(value, fallback=""):
    return value if isinstance(value, str) and value.strip() else fallback


def fixed(value, digits):
    with localcontext() as context:
        context.prec = 400
        return format(
            Decimal.from_float(float(value)).quantize(
                Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP
            ),
            f".{digits}f",
        )


def evidence_array(value):
    result = []
    for item in array(value):
        item = record(item)
        status = string(item.get("status"), "unavailable")
        result.append(
            {
                "source": string(item.get("source"), "unknown"),
                "status": status
                if status in ("available", "partial", "unavailable", "disabled")
                else "unavailable",
                "detail": string(item.get("detail")),
                "capturedAt": string(item.get("capturedAt"), now()),
                "metrics": record(item.get("metrics")),
            }
        )
    return result


def candidate_label(value):
    symbol = string(value.get("symbol")) or string(value.get("code"), "-")
    name = string(value.get("name"), "未命名")
    score, change = numeric(value.get("score")), numeric(value.get("change_percent"))
    return (
        name
        + " "
        + symbol
        + "："
        + ("无评分" if score is None else fixed(score, 1) + " 分")
        + "，最新涨跌 "
        + ("涨跌未知" if change is None else fixed(change, 2) + "%")
    )


def probe_metrics(value):
    value, result = record(value), {}
    for raw, key in (("universes", "universes"), ("candidates", "candidates")):
        if isinstance(value.get(raw), list):
            result[key] = len(value[raw])
    if type(value.get("scanned_symbols")) in (int, float):
        result["scannedSymbols"] = value["scanned_symbols"]
    if isinstance(value.get("source"), str):
        result["source"] = value["source"]
    analytics = record(value.get("analytics"))
    for raw, key in (("engine", "engine"), ("status", "analyticsStatus")):
        if analytics.get(raw):
            result[key] = analytics[raw]
    if string(value.get("status")):
        result["status"] = value["status"]
    return result


def build_report(watchlist, universes_probe, screener_probe, clickhouse_probe, *, date=None):
    evidence = [
        {**probe["evidence"], "source": source}
        for probe, source in (
            (universes_probe, "本地股票池"),
            (screener_probe, "短线候选筛选"),
            (clickhouse_probe, "ClickHouse 分析层"),
        )
    ]
    universes = [
        record(value) for value in array(record(universes_probe.get("data")).get("universes"))
    ]
    candidates = [
        record(value) for value in array(record(screener_probe.get("data")).get("candidates"))
    ]
    target = next(
        (item for item in universes if string(item.get("id")) == watchlist["universeId"]),
        universes[0] if universes else {},
    )
    analytics = record(record(screener_probe.get("data")).get("analytics"))
    unavailable = sum(item["status"] not in ("available", "partial") for item in evidence)
    ready, members = (
        coalesce(numeric(target.get("ready_count")), 0),
        coalesce(numeric(target.get("member_count")), len(watchlist["symbols"])),
    )
    ratio = ready / members if members > 0 else 0
    score = max(
        45,
        min(
            90,
            62
            + math.floor(min(18, ratio * 18) + 0.5)
            + min(12, len(candidates) * 2)
            - unavailable * 8,
        ),
    )
    risk = "high" if unavailable >= 2 or score < 65 else "medium" if score < 78 else "low"
    recommendation = (
        "数据不足，优先补齐证据后再行动"
        if unavailable >= 2
        else "谨慎积极，关注强势候选与仓位纪律"
        if score >= 82
        else "观察为主，等待候选信号确认"
        if score >= 70
        else "防守为主，先处理数据覆盖与风险暴露"
    )
    checklist = [
        "核对前 3 个候选标的的最新 K 线、成交额和涨跌停状态。"
        if candidates
        else "先检查筛选条件和本地行情覆盖，确认为何没有候选标的。",
        "股票池覆盖率可用于日报观察，继续保留补数监控。"
        if ratio >= 0.8
        else "优先补齐股票池日线覆盖，避免日报结论偏样本。",
        "ClickHouse 已参与筛选，可继续扩展横截面因子。"
        if analytics.get("engine") == "clickhouse"
        else "当前未确认 ClickHouse 命中，批量筛选仍需关注性能。",
        "任何买卖动作必须结合人工复核和风控仓位，不把日报作为即时交易指令。",
    ]
    date = date or datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y/%m/%d")
    title = date + " " + watchlist["name"] + " 投研日报"
    summary = (
        f"本次生成 {len(candidates)} 个候选标的，综合评分 {score}，建议：{recommendation}。"
        if candidates
        else f"本次未生成候选标的，综合评分 {score}，重点处理数据覆盖和筛选证据。"
    )
    candidate_lines = ["- " + candidate_label(value) for value in candidates[:5]] or [
        "- 暂无候选标的。"
    ]
    markdown = "\n".join(
        [
            "# " + title,
            "",
            "## 核心结论",
            summary,
            "",
            "## 综合评分",
            f"- 评分：{score}/100",
            "- 风险等级：" + {"low": "低", "medium": "中", "high": "高"}[risk],
            "- 建议：" + recommendation,
            "",
            "## 观察池",
            "- 股票池："
            + (watchlist["universeId"] if watchlist["universeId"] is not None else "未绑定"),
            "- 标的：" + ("、".join(watchlist["symbols"]) or "使用股票池成员"),
            "- 市场：" + ("、".join(watchlist["markets"]) or "未设置"),
            "",
            "## 候选标的",
            *candidate_lines,
            "",
            "## 证据来源",
            *(f"- {item['source']}: {item['status']}，{item['detail']}" for item in evidence),
            "",
            "## 操作检查清单",
            *("- " + item for item in checklist),
        ]
    )
    structured = {
        "version": 1,
        "watchlistId": watchlist["id"],
        "coverage": {
            "universeId": watchlist["universeId"],
            "readyCount": ready,
            "memberCount": members,
            "coverageRatio": float(fixed(ratio, 4)),
        },
        "candidates": [
            {
                **{
                    key: string(item.get(raw))
                    for key, raw in (
                        ("symbol", "symbol"),
                        ("code", "code"),
                        ("name", "name"),
                        ("tradeDate", "trade_date"),
                    )
                },
                "score": numeric(item.get("score")),
                "changePercent": numeric(item.get("change_percent")),
                "signals": [
                    value for value in array(item.get("signals")) if isinstance(value, str)
                ],
                "warnings": [
                    value for value in array(item.get("warnings")) if isinstance(value, str)
                ],
            }
            for item in candidates[:10]
        ],
        "analytics": {
            "engine": string(analytics.get("engine"), "unknown"),
            "status": string(analytics.get("status"), "unknown"),
            "basis": string(analytics.get("basis")),
        },
        "risks": checklist,
        "nextAdapters": [
            "llm-synthesis",
            "news-search",
            "sentiment-snapshot",
            "real-notification-delivery",
        ],
    }
    return {
        "title": title,
        "summary": summary,
        "score": score,
        "recommendation": recommendation,
        "riskLevel": risk,
        "contentMarkdown": markdown,
        "evidence": evidence,
        "structured": structured,
    }


def provider_matrix(evidence=None, channels=None):
    sources = {item["source"]: item for item in evidence or []}
    market, screener, clickhouse = (
        sources.get(key, {}) for key in ("本地股票池", "短线候选筛选", "ClickHouse 分析层")
    )
    return [
        {
            "id": "local-market-data",
            "name": "本地 market-data",
            "role": "股票池、行情覆盖、K 线和候选筛选的事实源",
            "status": market.get("status", "partial"),
            "detail": market.get("detail", "等待下一次日报运行采样。"),
        },
        {
            "id": "clickhouse",
            "name": "ClickHouse",
            "role": "全市场筛选、横截面因子和批量分析加速层",
            "status": clickhouse.get("status", "partial"),
            "detail": clickhouse.get("detail", "未采样时不判断健康状态。"),
        },
        {
            "id": "timescaledb",
            "name": "TimescaleDB",
            "role": "本地日线事实库和回测读取口径",
            "status": screener.get("status", "partial"),
            "detail": "当前筛选由 TimescaleDB 承接。"
            if record(screener.get("metrics")).get("engine") == "timescaledb"
            else "作为 ClickHouse 失败后的稳定回退路径。",
        },
        {
            "id": "news-sentiment",
            "name": "新闻与舆情源",
            "role": "后续接入企业新闻源、搜索 API 和社交舆情",
            "status": "disabled",
            "detail": "P0 先保留结构化证据位，不默认依赖付费 API。",
        },
        {
            "id": "notifications",
            "name": "推送通道",
            "role": "企业微信、飞书、钉钉、Telegram、Discord、邮件",
            "status": "partial" if channels else "disabled",
            "detail": f"已配置 {len(channels)} 个通道，当前默认 dry-run。"
            if channels
            else "尚未配置通道。",
        },
    ]
