import re
from datetime import UTC, date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

from .data_quality import assess_response
from .finance_values import coalesce, js_string, record
from .fundamentals import metric_comparison
from .portfolio import liquidity_summary
from .prefetch_planning import (
    candidate_code,
    history_limit,
    screener_limit,
    screener_mode,
    screener_trade_date,
    unique_symbols,
)
from .research_time import constrain_historical_query, research_time_for_plan
from .technical import calculate_metrics


def timestamp():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def screener_seed(plan, transport, artifacts, raw_files, warnings, assert_active=None):
    question = plan["question"]
    limit = screener_limit(question)
    time = research_time_for_plan(plan)
    trade_date = time["endDate"] if time else screener_trade_date(question)
    query = {"mode": screener_mode(question), "limit": str(limit)}
    if trade_date:
        query["trade_date"] = trade_date
    screener = await transport.fetch(
        "/api/v1/research/screeners/a-share/short-term-candidates",
        query=query,
        research_time=time,
        timeout_seconds=45,
    )
    if assert_active:
        await assert_active()
    raw_files.append(
        await artifacts.write(f"data_file/raw/{plan['runId']}/a-share-screener.json", screener)
    )
    symbols = unique_symbols(map(candidate_code, screener.get("candidates") or []))[:limit]
    quality = record(screener.get("data_quality")) or {}
    missing = [field for field in quality.get("missing_fields", []) if isinstance(field, str)]
    if missing:
        warnings.append("选股接口缺少字段：" + "、".join(missing))
    warnings.extend(
        "选股接口说明：" + note for note in screener.get("notes", []) if isinstance(note, str)
    )
    return {"symbols": symbols, "screener": screener}


def final_from_responses(
    symbol,
    quote,
    *,
    kline=None,
    technical=None,
    backtest=None,
    financials=None,
    fundamental=None,
    announcements=None,
    requested_time_range=None,
    now=None,
):
    kind = quote.get("asset_type") if isinstance(quote.get("asset_type"), str) else "stock"
    common = {
        "symbol": symbol,
        "asset_type": kind,
        **({"fetched_at": quote["fetched_at"]} if "fetched_at" in quote else {}),
    }
    kline = (
        kline
        if kline is not None
        else {
            **common,
            "bars": [],
            "data_quality": {
                "status": "warning",
                "missing_fields": ["bars"],
                "warnings": ["未获取历史 K 线。"],
            },
        }
    )

    def fallback(key, label):
        return {
            **common,
            key: [],
            "data_quality": {
                "status": "warning" if kind == "stock" else "ok",
                "missing_fields": [key] if kind == "stock" else [],
                "warnings": [
                    "未获取" + label + "。"
                    if kind == "stock"
                    else kind + " 标的默认不获取个股" + label + "。"
                ],
            },
        }

    financials = financials if financials is not None else fallback("reports", "财务摘要")
    announcements = (
        announcements if announcements is not None else fallback("announcements", "公告事件")
    )
    result = {
        "schemaVersion": 1,
        "generatedAt": now or timestamp(),
        "symbol": js_string(coalesce(quote.get("symbol"), symbol)),
        "name": quote.get("name") if isinstance(quote.get("name"), str) else None,
        **{key: quote[key] for key in ("secid", "market") if key in quote},
        "asset_type": kind,
        "source": coalesce(quote.get("source"), "eastmoney"),
        "currency": coalesce(quote.get("currency"), "CNY"),
        "timezone": coalesce(quote.get("timezone"), "Asia/Shanghai"),
        "quote": quote,
        "kline": kline,
        "technicalIndicators": technical,
        "backtest": backtest,
        "financials": financials,
        "fundamentalIndicators": fundamental,
        "fundamentalMetricComparison": metric_comparison(financials, requested_time_range),
        "announcements": announcements,
        "computedMetrics": calculate_metrics(kline),
        "liquidity": liquidity_summary([]),
    }
    if any(key in quote for key in ("as_of", "quote_time", "fetched_at")):
        result["as_of"] = coalesce(
            quote.get("as_of"), quote.get("quote_time"), quote.get("fetched_at")
        )
    return result


async def symbol_dataset(
    symbol, plan, transport, artifacts, raw_files, warnings, *, quote=None, assert_active=None
):
    # Only framework-resolved codes enter paths and endpoints.
    if not re.fullmatch(r"[6035][0-9]{5}", symbol):
        raise ValueError("Unsupported resolved symbol")
    time = research_time_for_plan(plan)

    async def active():
        if assert_active:
            await assert_active()

    def quality_warning(assessment):
        codes = "、".join(dict.fromkeys(issue["code"] for issue in assessment["issues"]))
        warnings.append(f"{symbol} 数据源存在质量或时效提示，请核对原始数据（{codes}）。")

    async def fetch(endpoint):
        await active()
        parsed = urlsplit(endpoint)
        query = dict(parse_qsl(parsed.query))
        if time and query.get("adjustment"):
            query["adjustment"] = "none"
        query = constrain_historical_query(parsed.path, query, time)
        assessments = []
        result = await transport.fetch(
            parsed.path + ("?" + urlencode(query) if query else ""),
            research_time=time,
            on_quality_warning=assessments.append,
        )
        await active()
        for assessment in assessments:
            quality_warning(assessment)
        return result

    async def save(filename, payload):
        raw_files.append(
            await artifacts.write(f"data_file/raw/{plan['runId']}/{symbol}/{filename}", payload)
        )
        return raw_files[-1]

    await active()
    limit = (
        (date.fromisoformat(time["endDate"]) - date.fromisoformat(time["startDate"])).days + 1
        if time and time.get("startDate")
        else history_limit(plan)
    )
    kline = None
    if time:
        kline = await fetch(f"/api/v1/quotes/history/{symbol}?limit={limit}")
        warnings.append(
            "历史行情按完整日线范围读取且不复权；价格修订、证券名称及行业成分尚无完整历史版本，不能视为全链路 Point-in-Time 认证。"
        )
        await save("kline-daily-none.json", kline)
    endpoint = f"/api/v1/quotes/realtime/{symbol}"
    assessment = assess_response(endpoint, quote, {}) if quote is not None else None
    usable = assessment and assessment["usable"]
    if quote is not None and not usable:
        warnings.append(f"{symbol} 批量行情不可用，已尝试单独获取。")
    if usable and assessment["status"] == "warning":
        quality_warning(assessment)
    last = record(kline["bars"][-1]) if kline and kline.get("bars") else None
    if time and last:
        quote = {
            **{
                key: kline[key]
                for key in (
                    "symbol",
                    "name",
                    "secid",
                    "market",
                    "asset_type",
                    "source",
                    "currency",
                    "timezone",
                    "fetched_at",
                    "data_quality",
                )
                if key in kline
            },
            "price": last.get("close"),
            "as_of": last.get("date"),
            "quote_time": last.get("date"),
            "price_basis": "historical_unadjusted_daily_close",
        }
    elif not usable:
        quote = await fetch(endpoint)
    await save("quote.json", quote)
    kind = quote.get("asset_type") if isinstance(quote.get("asset_type"), str) else "stock"
    requirements = plan.get("dataRequirements", [])

    def required(fragment):
        return any(fragment in item for item in requirements)

    async def optional(endpoint, filename, label):
        try:
            result = await fetch(endpoint)
            await save(filename, result)
            return result
        except Exception as cause:
            await active()
            warnings.append(f"{symbol} {label}预取失败：{cause}")
            return None

    if kline is None and required("/quotes/history/"):
        kline = await optional(
            f"/api/v1/quotes/history/{symbol}?period=daily&adjustment=qfq&limit={limit}",
            "kline-daily-qfq.json",
            "历史 K 线",
        )
    technical = None
    if (
        required("/indicators/technical/")
        or plan.get("capabilityId") == "asset_comparison"
        or plan.get("requestedCapabilityId") == "asset_comparison"
    ):
        technical = await optional(
            f"/api/v1/indicators/technical/{symbol}?period=daily&adjustment=qfq&limit={limit}",
            "technical-indicators.json",
            "技术指标",
        )
    backtest = None
    if required("/backtests/ma-crossover/"):
        try:
            backtest = await fetch(
                f"/api/v1/backtests/ma-crossover/{symbol}?fast_window=20&slow_window=60&period=daily&adjustment=qfq&limit={limit if time and time.get('startDate') else 250}&fee_bps=5"
            )
            experiment = record(backtest.get("experiment"))
            identity = experiment.get("experiment_id") if experiment else None
            suffix = (
                "-" + identity[7:]
                if isinstance(identity, str) and re.fullmatch("sha256:[a-f0-9]{64}", identity)
                else ""
            )
            path = await save("backtest-ma-crossover" + suffix + ".json", backtest)
            if experiment is not None:
                backtest = {
                    **{key: value for key, value in backtest.items() if key != "experiment"},
                    "experiment_ref": {
                        "artifact_path": path,
                        **{
                            key: experiment[key]
                            for key in ("experiment_id", "data_sha256", "result_sha256")
                            if isinstance(experiment.get(key), str)
                            and re.fullmatch("sha256:[a-f0-9]{64}", experiment[key])
                        },
                    },
                }
        except Exception as cause:
            await active()
            warnings.append(f"{symbol} 均线突破回测预取失败：{cause}")
    financials = fundamental = announcements = None
    if kind == "stock":
        if required("/fundamentals/financials/"):
            financials = await optional(
                f"/api/v1/fundamentals/financials/{symbol}?limit=8", "financials.json", "财务摘要"
            )
        if required("/indicators/fundamental/"):
            fundamental = await optional(
                f"/api/v1/indicators/fundamental/{symbol}?limit=8",
                "fundamental-indicators.json",
                "财务衍生指标",
            )
        if required("/events/announcements/"):
            announcements = await optional(
                f"/api/v1/events/announcements/{symbol}?limit=20", "announcements.json", "公告事件"
            )
    result = final_from_responses(
        symbol,
        quote,
        kline=kline,
        technical=technical,
        backtest=backtest,
        financials=financials,
        fundamental=fundamental,
        announcements=announcements,
        requested_time_range=plan.get("timeRange"),
    )
    return {
        **result,
        **({"researchTime": time, "historicalAvailability": "partial"} if time else {}),
    }
