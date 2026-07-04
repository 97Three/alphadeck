import math
import re

from .finance_values import coalesce, js_string, record
from .prefetch_market import timestamp
from .provenance import js_json

FINAL = "data_file/final/dashboard-data.json"
SOURCES = "evidence/sources.json"
QUALITY = "evidence/data_quality.json"


def pick_string(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if type(value) in {int, float} and math.isfinite(value):
            return js_string(value)
    return None


def present(value):
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def first_array(*values):
    return next((value for value in values if isinstance(value, list)), [])


def missing(value, groups):
    value = value or {}
    return [label for label, keys in groups if not any(present(value.get(key)) for key in keys)]


def dataset(
    identity, name, value, count, source, endpoint, critical, generated, fields, warnings=None
):
    value, warnings = value or {}, list(warnings or [])
    fetched = pick_string(value.get("fetched_at"), generated)
    if count <= 0:
        warnings.append("未检测到可用样本。")
    if fields:
        warnings.append("缺失字段：" + "、".join(fields) + "。")
    result = {
        "id": identity,
        "name": name,
        "source": source,
        "endpoint": endpoint,
        "artifact_path": FINAL,
        "row_count": count,
        "fetched_at": fetched,
        "as_of": pick_string(value.get("quote_time"), value.get("as_of"), fetched),
        "missing_fields": fields,
        "warnings": warnings,
        "status": "error" if count <= 0 and critical else "warning" if warnings else "ok",
        "critical": critical,
    }
    fetch = record(value.get("fetch"))
    if fetch is not None:
        projection = {
            key: pick_string(fetch.get(key))
            for key in ("cache_status", "cached_at", "expires_at")
            if pick_string(fetch.get(key))
        }
        ttl = fetch.get("cache_ttl_seconds")
        if type(ttl) in {int, float} and math.isfinite(ttl):
            projection["cache_ttl_seconds"] = ttl
        if projection:
            result["fetch"] = projection
    return result


def build_datasets(data, plan=None, *, depth=0):
    if depth > 8:
        raise ValueError("Nested asset evidence exceeds its depth budget")
    plan = plan or {}
    assets = (
        [item for item in data.get("assets", []) if isinstance(item, dict)]
        if isinstance(data.get("assets"), list)
        else []
    )
    if len(assets) > 1:
        result = []
        for asset in assets:
            quote = record(asset.get("quote")) or {}
            symbol = pick_string(asset.get("symbol"), quote.get("symbol"), "UNKNOWN")
            name = pick_string(asset.get("name"), quote.get("name"), symbol)
            for item in build_datasets(asset, plan, depth=depth + 1):
                result.append(
                    {
                        **item,
                        "id": symbol + "_" + item["id"],
                        "name": name + " " + item["name"],
                        "endpoint": item["endpoint"].replace("/UNKNOWN", "/" + symbol, 1),
                    }
                )
        return result
    screener = record(data.get("screener"))
    if not assets and screener is not None and isinstance(screener.get("candidates"), list):
        quality = record(screener.get("data_quality")) or {}
        warnings = [item for item in quality.get("warnings", []) if isinstance(item, str)]
        fields = [item for item in quality.get("missing_fields", []) if isinstance(item, str)]
        fetched = pick_string(
            screener.get("fetched_at"), data.get("generatedAt"), data.get("generated_at")
        )
        return [
            dataset(
                "a_share_screener",
                "A 股候选筛选",
                {
                    **screener,
                    "fetched_at": fetched,
                    "as_of": pick_string(
                        screener.get("trade_date"), screener.get("as_of"), fetched
                    ),
                },
                len(screener["candidates"]),
                pick_string(screener.get("source"), data.get("source"), "alphadeck-market-api"),
                "GET /api/v1/research/screeners/a-share/short-term-candidates?mode="
                + pick_string(screener.get("mode"), "short_term"),
                False,
                fetched,
                fields,
                warnings,
            )
        ]
    generated = pick_string(
        data.get("generatedAt"), data.get("generated_at"), data.get("fetched_at")
    )
    quote = record(data.get("quote")) or {}
    symbol = pick_string(data.get("symbol"), quote.get("symbol"), "UNKNOWN")
    source = pick_string(data.get("source"), quote.get("source"), "unknown")
    kind = pick_string(data.get("asset_type"), quote.get("asset_type"), "stock")
    capability = pick_string(plan.get("capabilityId"))
    critical = {"quote"}
    critical.update(
        {"kline"}
        if capability in {"technical_analysis", "portfolio_risk"}
        else {"financials"}
        if capability == "fundamental_analysis"
        else {"kline", "backtest"}
        if capability == "backtest_review"
        else {"kline", "financials"}
        if kind == "stock"
        else {"kline"}
    )

    def choose(*keys):
        return coalesce(*(record(data.get(key)) for key in keys), {})

    kline, financials, announcements = (
        choose("kline", "history"),
        choose("financials", "fundamentals"),
        choose("announcements", "events"),
    )
    fundamental, technical, backtest = (
        choose("fundamentalIndicators"),
        choose("technicalIndicators", "indicators"),
        choose("backtest"),
    )
    bars = first_array(kline.get("bars"), kline.get("data"), data.get("bars"), data.get("history"))
    reports = first_array(financials.get("reports"), financials.get("data"), data.get("reports"))
    notices = first_array(
        announcements.get("announcements"), announcements.get("data"), data.get("announcements")
    )
    points = first_array(technical.get("points"), technical.get("data"))
    fundamental_points = first_array(fundamental.get("points"), fundamental.get("data"))
    equity, trades = (
        first_array(backtest.get("equity_curve"), backtest.get("equityCurve")),
        first_array(backtest.get("trades")),
    )
    technical_summary = record(technical.get("summary"))
    stock = kind == "stock"
    requirements = [js_string(value) for value in plan.get("dataRequirements", [])]

    def required(fragment):
        return any(fragment in value for value in requirements)

    def at(values, index=0):
        return record(values[index]) if values else None

    def add(identity, name, value, count, endpoint, fields, warnings=None):
        # Empty dictionaries remain present objects, matching JS object semantics.
        return dataset(
            identity,
            name,
            value,
            count,
            pick_string((value or {}).get("source"), source),
            "GET /api/v1/" + endpoint + "/" + symbol,
            identity in critical or identity == "technical_indicators" and "kline" in critical,
            generated,
            fields,
            warnings,
        )

    time_fields = [("fetched_at", ["fetched_at", "as_of"])]
    result = [
        add(
            "quote",
            "实时行情",
            data.get("quote") if isinstance(data.get("quote"), dict) else None,
            1 if isinstance(data.get("quote"), dict) else 0,
            "quotes/realtime",
            missing(
                quote,
                [
                    ("symbol", ["symbol", "code"]),
                    ("price", ["price", "latest", "close"]),
                    ("quote_time/fetched_at", ["quote_time", "fetched_at", "as_of"]),
                    ("source", ["source"]),
                ],
            ),
        ),
        add(
            "financials",
            "财务摘要",
            financials,
            len(reports),
            "fundamentals/financials",
            missing(financials, time_fields)
            + missing(at(reports), [("report_date/period", ["report_date", "period", "date"])])
            if stock
            else [],
        ),
        add(
            "announcements",
            "公告事件",
            announcements,
            len(notices),
            "events/announcements",
            missing(announcements, time_fields)
            + missing(at(notices), [("title", ["title", "notice_title"])])
            if stock
            else [],
        ),
    ]
    if (
        bars
        or required("/quotes/history/")
        or capability in {"technical_analysis", "stock_diagnosis"}
    ):
        item = add(
            "kline",
            "历史 K 线",
            kline,
            len(bars),
            "quotes/history",
            missing(kline, [*time_fields, ("period", ["period"])])
            + missing(
                at(bars), [("trade_date/date", ["trade_date", "date"]), ("close", ["close"])]
            ),
        )
        item["endpoint"] += (
            "?period="
            + pick_string(kline.get("period"), "daily")
            + "&adjustment="
            + pick_string(kline.get("adjustment"), "qfq")
        )
        result.insert(1, item)
    if (
        isinstance(data.get("technicalIndicators"), dict)
        or isinstance(data.get("indicators"), dict)
        or required("/indicators/technical/")
    ):
        result.insert(
            2,
            add(
                "technical_indicators",
                "技术指标",
                technical,
                len(points) if points else 1 if technical_summary is not None else 0,
                "indicators/technical",
                missing(technical, time_fields)
                + missing(
                    coalesce(at(points, -1), technical_summary),
                    [("date", ["date"]), ("ma5/ma20", ["ma5", "ma20"])],
                ),
            ),
        )
    if stock and (
        isinstance(data.get("fundamentalIndicators"), dict) or required("/indicators/fundamental/")
    ):
        summary = record(financials.get("summary")) or {}
        computed = any(
            present(summary.get(key))
            for key in (
                "latest_net_margin",
                "avg_roe",
                "avg_gross_margin",
                "avg_net_margin",
                "latest_weighted_roe",
                "latest_gross_margin",
            )
        )
        direct = isinstance(data.get("fundamentalIndicators"), dict) or bool(fundamental_points)
        fallback = (
            {
                "source": pick_string(financials.get("source"), source) + " + dashboard-computed",
                "fetched_at": pick_string(
                    financials.get("fetched_at"), financials.get("as_of"), generated
                ),
                "as_of": pick_string(
                    financials.get("as_of"), financials.get("fetched_at"), generated
                ),
            }
            if computed
            else None
        )
        item = add(
            "fundamental_indicators",
            "财务衍生指标",
            fundamental if direct else fallback,
            len(fundamental_points) if direct else 1 if computed else 0,
            "indicators/fundamental",
            missing(fundamental, time_fields)
            + missing(
                at(fundamental_points),
                [
                    ("report_date", ["report_date"]),
                    ("net_margin/roe", ["net_margin", "weighted_roe"]),
                ],
            )
            if direct
            else [],
            []
            if direct
            else [
                "独立财务衍生指标接口未返回可用样本；已基于真实财务报表 reports 计算净利率、平均 ROE、平均毛利率和平均净利率。",
                "衍生指标口径依赖 final 数据中的财报字段，需结合后续正式披露持续更新。",
            ]
            if computed
            else ["未检测到可计算的财务衍生指标；页面需说明该数据缺口。"],
        )
        if not direct:
            item["endpoint"] = (
                "GET /api/v1/fundamentals/financials/" + symbol + " -> financials.summary"
            )
        result.insert(
            next(
                (i for i, value in enumerate(result) if value["id"] == "announcements"), len(result)
            ),
            item,
        )
    if (
        isinstance(data.get("backtest"), dict)
        or required("/backtests/ma-crossover/")
        or capability == "backtest_review"
    ):
        fields = (
            missing(backtest, [*time_fields, ("summary", ["summary"])])
            + missing(at(equity, -1), [("date", ["date"]), ("equity", ["equity"])])
            + (["trades"] if not trades and "backtest" in critical else [])
        )
        item = add(
            "backtest",
            "均线突破回测",
            backtest,
            len(equity),
            "backtests/ma-crossover",
            fields,
            [] if trades else ["样本区间内未检测到完整交易，需在页面说明策略信号不足。"],
        )
        result.insert(
            next((i for i, value in enumerate(result) if value["id"] == "financials"), len(result)),
            item,
        )
    return (
        result
        if stock
        else [value for value in result if value["id"] not in {"financials", "announcements"}]
    )


async def ensure_baseline(artifacts, *, force=False, now=None):
    sources, quality = artifacts.read(SOURCES), artifacts.read(QUALITY)
    if (
        not force
        and sources
        and isinstance(sources.get("sources"), list)
        and sources["sources"]
        and re.search("source|endpoint|fetched_at|as_of|artifact_path", js_json(sources), re.I)
        and quality
        and quality.get("status") in {"ok", "warning", "error"}
        and re.search(
            "datasets|checks|missing_fields|warnings|limitations|row_count|fetched_at",
            js_json(quality),
            re.I,
        )
    ):
        return {
            "created": False,
            "status": quality["status"],
            "sourceCount": len(sources["sources"]),
        }
    data = artifacts.read(FINAL)
    if data is None:
        return {"created": False, "reason": "未找到可用于生成 evidence 的 " + FINAL + "。"}
    plan = artifacts.read(".data-agent/finance-run-plan.json") or {}
    now = now or timestamp()
    run = pick_string(
        plan.get("runId"), plan.get("run_id"), data.get("runId"), data.get("generatedAt"), now
    )
    quote = record(data.get("quote")) or {}
    symbol = pick_string(data.get("symbol"), quote.get("symbol"), "UNKNOWN")
    name = pick_string(data.get("name"), quote.get("name"), symbol)
    datasets = build_datasets(data, plan)
    status = (
        "error"
        if any(value["status"] == "error" for value in datasets)
        else "warning"
        if any(value["status"] == "warning" for value in datasets)
        else "ok"
    )
    common = {
        "schemaVersion": 1,
        "runId": run,
        "generated_by": "alphadeck-platform",
        "created_at": now,
        "symbol": symbol,
        "name": name,
    }
    source_rows = [
        {
            "id": value["id"],
            "dataset": value["name"],
            **{
                key: value[key]
                for key in (
                    "source",
                    "endpoint",
                    "artifact_path",
                    "row_count",
                    "fetched_at",
                    "as_of",
                    "status",
                    "fetch",
                )
                if key in value
            },
        }
        for value in datasets
    ]
    await artifacts.write(SOURCES, {**common, "sources": source_rows})
    await artifacts.write(
        QUALITY,
        {
            **common,
            "status": status,
            "datasets": [
                {
                    **{key: item for key, item in value.items() if key != "critical"},
                    "required": value["critical"],
                }
                for value in datasets
            ],
            "checks": [
                {
                    "id": value["id"] + "_quality",
                    "dataset": value["id"],
                    "status": value["status"],
                    "row_count": value["row_count"],
                    "missing_fields": value["missing_fields"],
                    "summary": value["name"]
                    + (
                        "数据形态可用。"
                        if value["status"] == "ok"
                        else "存在质量提示，需要在页面或结论中说明。"
                    ),
                }
                for value in datasets
            ],
            "warnings": [
                value["name"] + "：" + warning
                for value in datasets
                for warning in value["warnings"]
            ],
            "limitations": [
                "东方财富等公开接口可能存在延迟，实时性以 fetched_at 与 quote_time/as_of 为准。",
                "本 evidence 为 AlphaDeck 平台根据最终数据文件自动生成的基础证据，模型可在后续分析中继续补充更细的数据口径说明。",
            ],
        },
    )
    await artifacts.event(
        {
            "event_type": "data_quality_checked",
            "stage": "data_quality",
            "status": "success" if status == "ok" else status,
            "run_id": run,
            "artifact_path": QUALITY,
            "summary": f"已自动生成数据质量证据，状态：{status}，来源数量：{len(datasets)}。",
            "created_at": now,
        }
    )
    return {"created": True, "status": status, "sourceCount": len(datasets)}
