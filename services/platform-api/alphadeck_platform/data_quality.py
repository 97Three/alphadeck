import math
import re
from datetime import datetime
from urllib.parse import unquote

from .research_time import calendar_day

UNSET = object()


def record(value):
    return value if isinstance(value, dict) else {}


def number(value):
    if type(value) in {int, float}:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except OverflowError:
            return None
    if (
        not isinstance(value, str)
        or len(value) > 128
        or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?", value, re.I)
    ):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def timestamp(value):
    if (
        not isinstance(value, str)
        or not re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:$|[T ])", value)
        or not calendar_day(value[:10])
    ):
        return None
    try:
        parsed = datetime.fromisoformat(
            (value + "T00:00:00Z" if len(value) == 10 else value).replace("Z", "+00:00")
        )
        return math.floor(parsed.timestamp() * 1000)
    except ValueError:
        return None


def day(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{8}", value):
        value = value[:4] + "-" + value[4:6] + "-" + value[6:]
    return value if calendar_day(value) else None


def security_code(value):
    match = (
        re.fullmatch(r"(?:(?:SH|SZ|BJ)[.:])?([0-9]{6})(?:\.(?:SH|SZ|BJ))?", value, re.I)
        if isinstance(value, str)
        else None
    )
    return match[1] if match else None


def explicit_market(value):
    match = (
        re.fullmatch(r"(SH|SZ|BJ)[.:][0-9]{6}|[0-9]{6}\.(SH|SZ|BJ)", value, re.I)
        if isinstance(value, str)
        else None
    )
    return (match[1] or match[2]).upper() if match else None


def assess_temporal(path, payload, query=None):
    query, issues = query or {}, []

    def issue(code, location, severity="error"):
        if len(issues) < 24:
            issues.append({"code": code, "path": location, "severity": severity})

    if "as_of" in query and re.search(
        r"/(?:fundamentals/financials|indicators/fundamental)/", path
    ):
        cutoff, knowledge = timestamp(query["as_of"]), record(payload.get("knowledge"))
        if (
            cutoff is None
            or timestamp(payload.get("as_of")) != cutoff
            or timestamp(knowledge.get("cutoff")) != cutoff
            or knowledge.get("point_in_time") is not True
            or not isinstance(knowledge.get("data_version"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", knowledge["data_version"])
        ):
            issue("historical_snapshot_mismatch", "knowledge")
        vintages = knowledge.get("vintages")
        if not isinstance(vintages, list):
            issue("historical_vintages_missing", "knowledge.vintages")
        else:
            for value in vintages:
                for key in ("observed_at", "available_at"):
                    point = timestamp(record(value).get(key))
                    if point is None or (cutoff is not None and point > cutoff):
                        issue("vintage_after_cutoff", "knowledge.vintages." + key)
    if (
        "trade_date" in query
        and path.endswith("/screeners/a-share/short-term-candidates")
        and payload.get("trade_date") != query["trade_date"]
    ):
        issue("historical_trade_date_mismatch", "trade_date")
    if "end" not in query or not re.search(
        r"/(?:quotes/history|indicators/technical|backtests/(?:ma-crossover|strategies/[^/]+))/",
        path,
    ):
        return issues
    upper, lower = day(query["end"]), day(query["start"]) if "start" in query else None
    if not upper or ("start" in query and (not lower or lower > upper)):
        issue("historical_window_invalid", "$query")
        return issues
    if query.get("period") == "daily" and payload.get("period") != "daily":
        issue("historical_period_missing", "period")
    if query.get("adjustment") == "none" and payload.get("adjustment") != "none":
        issue("historical_adjustment_missing", "adjustment")

    def check_date(value, location):
        date = day(value)
        if not date or date > upper or (lower and date < lower):
            issue("outside_research_window", location)

    key = (
        "bars"
        if "/quotes/history/" in path
        else "points"
        if "/indicators/technical/" in path
        else "equity_curve"
    )
    rows = payload.get(key)
    if not isinstance(rows, list) or not rows:
        issue("historical_series_missing", key)
    else:
        for index, value in enumerate(rows):
            check_date(record(value).get("date"), f"{key}[{index}].date")
    if isinstance(payload.get("trades"), list):
        for index, value in enumerate(payload["trades"]):
            trade = record(value)
            check_date(trade.get("entry_date"), f"trades[{index}].entry_date")
            if trade.get("exit_date") is not None:
                check_date(trade["exit_date"], f"trades[{index}].exit_date")
    issue("historical_trade_dates_only", "metadata", "warning")
    return issues


def assess_response(path, payload, query=None):
    query, issues = query or {}, []
    count, failed, usable, row_count = 0, False, True, None

    def issue(code, location, severity="error"):
        nonlocal count, failed
        count += 1
        failed = failed or severity == "error"
        if len(issues) < 24:
            issues.append({"code": code, "path": location, "severity": severity})

    if not isinstance(payload, dict):
        issue("response_not_object", "$")
    else:
        for item in assess_temporal(path, payload, query):
            issue(item["code"], item["path"], item["severity"])
        if (
            payload.get("ok") is False
            or payload.get("success") is False
            or payload.get("error") is not None
        ):
            issue("api_error_payload", "$")
        quality = record(payload.get("data_quality"))
        if "data_quality" in payload and quality.get("status") not in ("ok", "warning", "error"):
            issue("quality_status_invalid", "data_quality.status")
        if quality.get("status") == "error":
            issue("source_quality_error", "data_quality.status")
        elif quality.get("status") == "warning":
            issue("source_quality_warning", "data_quality.status", "warning")
        if payload.get("stale") is True or quality.get("stale") is True:
            issue("source_stale", "data_quality", "warning")
        entity = re.fullmatch(
            r"/api/v1/(?:quotes/(?:history|realtime)|research/bars|indicators/(?:technical|fundamental)|fundamentals/financials|events/(?:announcements|dividends)|backtests/(?:ma-crossover|strategies/[^/]+))/([^/]+)",
            path,
        )
        expected = security_code(unquote(entity[1])) if entity else None
        if expected:
            actual = security_code(payload.get("symbol"))
            if not actual:
                issue("symbol_missing", "symbol")
            elif actual != expected:
                issue("symbol_mismatch", "symbol")
            expected_market, symbol_market = (
                explicit_market(unquote(entity[1])),
                explicit_market(payload.get("symbol")),
            )
            declared = (
                payload.get("market")
                if payload.get("market") is not None
                else payload.get("exchange")
            )
            market = (
                declared.upper()
                if isinstance(declared, str) and re.fullmatch(r"SH|SZ|BJ", declared, re.I)
                else symbol_market
            )
            if expected_market and not market:
                issue("market_missing", "market")
            elif (expected_market and market != expected_market) or (
                symbol_market and market != symbol_market
            ):
                issue("market_mismatch", "market")
        if re.fullmatch(r"/api/v1/quotes/realtime/[^/]+", path):
            price = number(payload.get("price"))
            if payload.get("price") is None or price == 0:
                usable = False
                issue("quote_price_missing", "price", "warning")
            elif price is None or price < 0:
                issue("quote_price_invalid", "price")
        for key in ("adjustment", "period"):
            actual = payload.get(key)
            actual = (
                payload.get("timeframe", UNSET)
                if key == "period" and actual is None
                else UNSET
                if actual is None
                else actual
            )
            if isinstance(query.get(key), str) and actual is not UNSET and actual != query[key]:
                issue(key + "_mismatch", key)
        if re.match(r"/api/v1/(?:quotes/history|research/bars)/", path):
            bars = payload.get("bars")
            if not isinstance(bars, list):
                issue("bars_missing", "bars")
            else:
                row_count = len(bars)
                if not row_count:
                    usable = False
                    issue("empty_series", "bars", "warning")
                if row_count > 20000:
                    issue("series_limit_exceeded", "bars")
                previous, seen = None, set()
                as_of = timestamp(payload.get("as_of"))
                as_of_end = (
                    as_of + 86399999 if as_of is not None and len(payload["as_of"]) == 10 else as_of
                )
                for index, bar in enumerate(bars[:20000]):
                    prefix = f"bars[{index}]"
                    if not isinstance(bar, dict):
                        issue("bar_not_object", prefix)
                        continue
                    point = timestamp(
                        next(
                            (
                                bar[key]
                                for key in ("date", "ts", "trade_date")
                                if bar.get(key) is not None
                            ),
                            None,
                        )
                    )
                    if point is None:
                        issue("bar_time_invalid", prefix + ".date")
                    else:
                        if point in seen:
                            issue("duplicate_bar_time", prefix + ".date")
                        elif previous is not None and point < previous:
                            issue("bar_time_out_of_order", prefix + ".date")
                        if as_of_end is not None and point > as_of_end:
                            issue("bar_after_as_of", prefix + ".date")
                        seen.add(point)
                        previous = point
                    prices = {}
                    for field in ("open", "high", "low", "close", "volume", "amount"):
                        if bar.get(field) is None:
                            if field == "close":
                                usable = False
                                issue("close_missing", prefix + ".close", "warning")
                            continue
                        parsed = number(bar[field])
                        if parsed is None or parsed < 0:
                            issue("invalid_market_number", prefix + "." + field)
                        else:
                            prices[field] = parsed
                    if "high" in prices and "low" in prices and prices["high"] < prices["low"]:
                        issue("invalid_ohlc_range", prefix)
                    for field in ("open", "close"):
                        if field in prices and (
                            ("high" in prices and prices[field] > prices["high"])
                            or ("low" in prices and prices[field] < prices["low"])
                        ):
                            issue("price_outside_ohlc_range", prefix + "." + field)
    return {
        "version": 1,
        "status": "failed" if failed else "warning" if count else "passed",
        "usable": usable and not failed,
        "rowCount": row_count,
        "issues": issues,
        "issueCount": count,
    }


def assess_dashboard(value):
    if not isinstance(value, dict):
        return []
    datasets = [
        value,
        *(
            [item for item in value["assets"] if isinstance(item, dict)]
            if isinstance(value.get("assets"), list)
            else []
        ),
    ]
    result = []
    for asset in datasets:
        kline = asset.get("kline") if asset.get("kline") is not None else asset.get("history")
        if isinstance(kline, dict):
            result.append(
                assess_response("/api/v1/quotes/history/" + str(asset.get("symbol", "")), kline)
            )
    return result
