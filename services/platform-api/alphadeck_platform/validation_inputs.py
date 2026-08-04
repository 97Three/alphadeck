import json
import re

from .finance_values import coalesce, numeric, record
from .prefetch_planning import pick_symbol, unique_symbols
from .scaffold import controlled_path
from .skill_integrity import read_regular
from .visualization import catalogue


def read_text(root, relative, maximum=8388608):
    try:
        return read_regular(controlled_path(root, relative), maximum).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def read_json(root, relative):
    raw = read_text(root, relative)
    try:
        return json.loads(raw) if raw is not None else None
    except ValueError:
        return None


def read_plan(root):
    return record(read_json(root, ".data-agent/finance-run-plan.json"))


def text_for_intent(value, depth=0):
    if depth > 32:
        raise ValueError("Intent payload exceeds depth budget")
    if isinstance(value, str):
        return re.sub(r"\s+", "", value)
    if isinstance(value, (dict, list)):
        return "".join(
            text_for_intent(item, depth + 1)
            for item in (value.values() if isinstance(value, dict) else value)
        )
    return ""


def any_key_deep(value, keys, depth=0):
    if depth > 32:
        return False
    if isinstance(value, list):
        return any(any_key_deep(item, keys, depth + 1) for item in value)
    return isinstance(value, dict) and any(
        key in keys or any_key_deep(item, keys, depth + 1) for key, item in value.items()
    )


def expected_template(plan):
    return (catalogue()["TEMPLATE_BY_CAPABILITY"].get((plan or {}).get("capabilityId")) or {}).get(
        "id"
    )


def planned_symbols(plan):
    values = (plan or {}).get("symbols")
    return unique_symbols(map(pick_symbol, values if isinstance(values, list) else []))


def fetched_symbols(value):
    value = record(value) or {}
    assets = (
        [item for item in value.get("assets", []) if isinstance(item, dict)]
        if isinstance(value.get("assets"), list)
        else []
    )
    candidates = (
        [
            coalesce(
                pick_symbol(asset), pick_symbol((record(asset.get("quote")) or {}).get("symbol"))
            )
            for asset in assets
        ]
        if assets
        else [
            pick_symbol(value),
            pick_symbol(value.get("quote")),
            *(map(pick_symbol, value["symbols"]) if isinstance(value.get("symbols"), list) else []),
        ]
    )
    return unique_symbols(candidates)


def comparison_symbols(value):
    value = record(value) or {}
    comparison = value.get("comparison")
    rows = (
        comparison.get("rows")
        if isinstance(comparison, dict)
        else comparison
        if isinstance(comparison, list)
        else []
    )
    return unique_symbols(map(pick_symbol, rows if isinstance(rows, list) else []))


def records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def inspect_payload(value, depth=0):
    if depth > 8:
        raise ValueError("Nested dashboard data exceeds depth budget")
    value = record(value) or {}
    assets = records(value.get("assets"))
    if assets:
        children = [inspect_payload(item, depth + 1) for item in assets]
        quote, count = (
            any(item["hasQuote"] for item in children),
            sum(item["barCount"] for item in children),
        )
    else:
        kline = coalesce(*(record(value.get(key)) for key in ("kline", "history", "ohlc")), {})
        bars = next(
            (
                records(candidate)
                for candidate in [
                    *(kline.get(key) for key in ("bars", "data", "items")),
                    *(value.get(key) for key in ("bars", "klines", "candles", "history")),
                ]
                if records(candidate)
            ),
            [],
        )
        prices = record(value.get("quote")) or {}
        quote = any(
            numeric(item) is not None
            for item in [
                *(prices.get(key) for key in ("price", "latest", "latest_price", "close")),
                *(value.get(key) for key in ("price", "latest", "latest_price")),
            ]
        )
        count = len(bars)
    return {
        "hasQuote": quote,
        "barCount": count,
        "fetchedSymbols": fetched_symbols(value),
        "hasUsableMarketData": quote or count > 0,
    }


def empty_screener(value):
    value = record(value) or {}
    screener = record(value.get("screener")) or {}
    from .baseline_evidence import pick_string

    return bool(
        value.get("status") == "no_candidates"
        and value.get("assets") == []
        and screener.get("candidates") == []
        and numeric(screener.get("total_candidates")) == 0
        and pick_string(screener.get("source"))
        and pick_string(
            coalesce(screener.get("fetched_at"), screener.get("as_of"), screener.get("trade_date"))
        )
        and all(
            isinstance((record(value.get(key)) or {}).get("rows"), list)
            for key in ("comparison", "selectionRanking", "financialQuality", "tradingPlan")
        )
        and isinstance(value.get("warnings"), list)
        and len(value["warnings"]) > 0
    )
