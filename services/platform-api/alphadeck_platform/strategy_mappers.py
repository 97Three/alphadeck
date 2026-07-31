"""Typed market-to-platform projections; field contracts are stored as declarative data."""

import json
import math
import re

from .config import ROOT
from .finance_values import coalesce, js_string, left_sum, numeric
from .generation_state import now

CATALOG = json.loads((ROOT / "config/strategy-catalog.json").read_text())
FIELDS = CATALOG["mapperFields"]
ENUMS = {
    "dataStatus": ({"ready", "stale", "missing"}, "missing"),
    "trendStatus": ({"bullish", "bearish", "sideways", "insufficient"}, "insufficient"),
    "screenerMode": ({"limit_up_relay", "trend_liquidity", "short_term"}, "short_term"),
    "foundationStatus": ({"ready", "partial", "missing"}, "partial"),
    "dataQualityIssueSeverity": ({"ok", "warning", "error"}, "warning"),
}
SECTORS = [
    (r"银行", "银行"),
    (r"证券|国泰君安|华泰|广发", "证券"),
    (r"保险|中国平安|太保", "保险"),
    (r"茅台|五粮液|泸州老窖|汾酒|今世缘", "白酒"),
    (r"通富|圣邦|紫光|华润微|中芯|韦尔|兆易|卓胜|汇顶", "半导体"),
    (r"三七|完美世界|世纪华通|掌趣|昆仑万维", "游戏"),
    (r"宁德|比亚迪|电池|先导智能", "新能源车"),
    (r"美的|格力|家电|三花", "家电"),
    (r"黄金", "黄金珠宝"),
    (r"联通|中兴|通信|移远|广和通", "通信服务"),
    (r"石油|石化|石化|荣盛石化|东方盛虹", "石油石化"),
    (r"医药|恒瑞", "医药"),
    (r"电力|长江电力", "电力"),
    (r"光伏|隆基", "光伏"),
]


def record(value):
    return value if isinstance(value, dict) else {}


def string(value, fallback=""):
    return value if isinstance(value, str) else fallback


def array(value):
    return value if isinstance(value, list) else []


def strings(value, *, trim=True):
    values = [js_string(item) for item in array(value)]
    return (
        [
            value.strip()
            for value in values
            if value.strip() and value.strip() not in ("-", "--", "无", "暂无")
        ]
        if trim
        else [value for value in values if value]
    )


def boolean(value):
    if type(value) is bool:
        return value
    if type(value) in (int, float):
        return True if value == 1 else False if value == 0 else None
    if isinstance(value, str):
        value = value.strip().lower()
        return (
            True
            if value in ("1", "true", "t", "yes", "y")
            else False
            if value in ("0", "false", "f", "no", "n")
            else None
        )
    return None


def lookup(value, path):
    for key in path:
        value = record(value).get(key)
    return value


def fields(name, value):
    result = {}
    for key, definition in FIELDS[name].items():
        raw = lookup(value, definition["path"])
        fallback = definition.get("fallback")
        if isinstance(fallback, dict) and fallback.get("clock"):
            fallback = now()
        operation = definition["op"]
        if operation == "asNumber":
            result[key] = coalesce(numeric(raw), fallback)
        elif operation == "asString":
            result[key] = string(raw, fallback if "fallback" in definition else "")
        elif operation == "nullableString":
            result[key] = string(raw, None)
        elif operation == "asStringArray":
            result[key] = strings(raw)
        elif operation == "asBoolean":
            result[key] = boolean(raw)
        elif operation == "asRecord":
            result[key] = record(raw)
        elif operation in ENUMS:
            allowed, default = ENUMS[operation]
            result[key] = raw if isinstance(raw, str) and raw in allowed else default
        else:
            raise ValueError("Unknown strategy field transform")
    return result


def mapped(name, value, *args):
    value = record(value)
    if name in ("mapSectorCapitalFlowMarketSummary", "mapSectorCapitalFlowDetail") and not value:
        return None
    result = fields(name, value)

    def children(key, mapper):
        return [mapped(mapper, item) for item in array(value.get(key))]

    if name == "mapResearchMember":
        result.update(
            {
                key: string(value.get(raw), None)
                for key, raw in (
                    ("name", "name"),
                    ("industry", "industry"),
                    ("region", "region"),
                    ("sectorHint", "sector_hint"),
                )
            }
        )
        concepts = strings(value.get("concepts"))
        inferred = next(
            ([sector] for pattern, sector in SECTORS if re.search(pattern, result["name"] or "")),
            [],
        )
        tags = [result["industry"], *concepts[:3], result["region"], *inferred]
        result.update(
            concepts=concepts,
            sectorTags=strings(value.get("sector_tags"))
            or list(dict.fromkeys(item.strip() for item in tags if item and item.strip())),
        )
    elif name == "mapResearchUniverse":
        members = children("members", "mapResearchMember")
        result.update(
            members=members, memberCount=coalesce(numeric(value.get("member_count")), len(members))
        )
    elif name == "mapResearchUniverseMembersPage":
        universe, fallback_page, fallback_size = args
        members = children("members", "mapResearchMember")
        total, page_size = (
            coalesce(numeric(value.get("total")), len(members)),
            coalesce(numeric(value.get("page_size")), fallback_size),
        )
        pages = max(1, math.ceil(total / page_size)) if page_size else None
        result.update(
            universeId=string(value.get("universe_id"), universe),
            page=coalesce(numeric(value.get("page")), fallback_page),
            pageSize=page_size,
            total=total,
            totalPages=coalesce(numeric(value.get("total_pages")), pages),
            members=members,
        )
    elif name == "mapAnalyticsExecutionMetadata":
        engine = "clickhouse" if value.get("engine") == "clickhouse" else "timescaledb"
        status = (
            value.get("status")
            if value.get("status") in ("hit", "fallback", "disabled", "error")
            else "hit"
            if engine == "clickhouse"
            else "fallback"
        )
        sync = (
            value.get("auto_sync_status")
            if value.get("auto_sync_status") in ("synced", "skipped", "error")
            else "not_needed"
        )
        result.update(
            engine=engine,
            status=status,
            basis=string(value.get("basis"), args[0] if args else ""),
            autoSyncStatus=sync,
        )
    elif name == "mapScreenerResponse":
        candidates = children("candidates", "mapScreenerCandidate")
        basis = string(value.get("data_basis"), "timescaledb.stock_bars")
        result.update(
            totalCandidates=coalesce(numeric(value.get("total_candidates")), len(candidates)),
            limit=coalesce(numeric(value.get("limit")), len(candidates)),
            candidates=candidates,
            dataBasis=basis,
            analytics=mapped("mapAnalyticsExecutionMetadata", value.get("analytics"), basis),
        )
    elif name == "mapLocalKlineBar":
        meta = record(value.get("metadata"))
        result.update(
            ts=string(value.get("ts"), string(value.get("date"))),
            provider=string(
                value.get("provider"),
                string(value.get("source"), string(meta.get("source"), "unknown")),
            ),
        )
        if meta:
            result["metadata"] = meta
    elif name == "mapDividendEventsResponse":
        result["events"] = children("events", "mapDividendEvent")
    elif name == "mapSectorCapitalFlowItem":
        signal = string(value.get("signal"), "insufficient")
        result.update(
            signal=signal
            if signal in ("warming", "cooling", "neutral", "insufficient")
            else "insufficient",
            topSymbols=strings(value.get("top_symbols"), trim=False),
        )
    elif name == "mapSectorCapitalFlowMarketSummary":
        result.update(
            {
                key: strings(value.get(raw), trim=False)
                for key, raw in (
                    ("strongestSectors", "strongest_sectors"),
                    ("weakestSectors", "weakest_sectors"),
                    ("analysis", "analysis"),
                )
            }
        )
    elif name == "mapSectorCapitalFlowDetail":
        result.update(
            item=mapped("mapSectorCapitalFlowItem", value.get("item")),
            trend=children("trend", "mapSectorCapitalFlowTrendPoint"),
            topMembers=children("top_members", "mapSectorCapitalFlowMember"),
            analysis=strings(value.get("analysis"), trim=False),
        )
    elif name == "mapSectorCapitalFlowResponse":
        result.update(
            items=children("items", "mapSectorCapitalFlowItem"),
            marketSummary=mapped("mapSectorCapitalFlowMarketSummary", value.get("market_summary")),
            detail=mapped("mapSectorCapitalFlowDetail", value.get("detail")),
        )
    elif name == "mapLocalKlineResponse":
        bars, summary = children("bars", "mapLocalKlineBar"), record(value.get("summary"))
        first, latest, previous = (
            bars[0] if bars else {},
            bars[-1] if bars else {},
            bars[-2] if len(bars) >= 2 else {},
        )
        timeframe = string(value.get("timeframe"), string(value.get("period"), "daily"))
        previous_close = coalesce(
            numeric(summary.get("previous_close")),
            latest.get("previousClose"),
            None if timeframe.startswith("minute") else previous.get("close"),
        )
        amounts = [bar["amount"] for bar in bars if bar["amount"] is not None]
        result.update(
            exchange=string(value.get("exchange"), string(value.get("market"), "UNKNOWN")),
            provider=string(value.get("provider"), string(value.get("source")) or None),
            dataProvider=latest.get("provider"),
            timeframe=timeframe,
            bars=bars,
            summary={
                "rowCount": coalesce(numeric(summary.get("row_count")), len(bars)),
                "firstTs": string(summary.get("first_ts"), first.get("ts")),
                "lastTs": string(summary.get("last_ts"), latest.get("ts")),
                "latestClose": coalesce(numeric(summary.get("latest_close")), latest.get("close")),
                "previousClose": previous_close,
                "returnPct": coalesce(
                    numeric(summary.get("return_pct")),
                    ((latest["close"] - previous_close) / previous_close) * 100
                    if latest and previous_close
                    else None,
                ),
                "high": coalesce(
                    numeric(summary.get("high")), max((bar["high"] for bar in bars), default=None)
                ),
                "low": coalesce(
                    numeric(summary.get("low")), min((bar["low"] for bar in bars), default=None)
                ),
                "totalVolume": coalesce(
                    numeric(summary.get("total_volume")), left_sum(bar["volume"] for bar in bars)
                ),
                "totalAmount": coalesce(
                    numeric(summary.get("total_amount")), left_sum(amounts) if amounts else None
                ),
            },
        )
    elif name == "mapIngestionJob":
        result["metadata"] = record(value.get("metadata"))
    elif name == "mapIngestionJobsResponse":
        result["jobs"] = children("jobs", "mapIngestionJob")
    elif name == "mapTradingCalendarDay":
        result["isOpen"] = value.get("is_open") is not False
    elif name == "mapDataQualityScan":
        result["issues"] = children("issues", "mapDataQualityIssue")
    elif name == "mapIngestionJobControlResult":
        action = string(value.get("action"))
        result["action"] = action if action in ("pause", "resume", "stop") else "pause"
    return result
