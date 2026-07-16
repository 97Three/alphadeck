import math
import re

from .provenance import js_json


def record(value):
    return value if isinstance(value, dict) else None


def numeric(value):
    if type(value) in {int, float}:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except OverflowError:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+", value):
            parsed = float(int(value, 0))
            return parsed if math.isfinite(parsed) else None
        if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (ValueError, OverflowError):
        return None


def left_sum(values):
    result = 0
    for value in values:
        result += value
    return result


def mean(values):
    return left_sum(values) / len(values) if values else None


def rounded(value, digits=2):
    if value is None or not math.isfinite(value):
        return None
    factor = 10**digits
    raw = value * factor
    if not math.isfinite(raw):
        return None
    lower = math.floor(raw)
    return (lower + int(raw - lower >= 0.5)) / factor


def bars_from_asset(asset):
    if not isinstance(asset, dict):
        return []
    kline = record(asset.get("kline"))
    kline = record(asset.get("history")) if kline is None else kline
    candidates = [(kline or {}).get(key) for key in ("bars", "data", "items")] + [
        asset.get(key) for key in ("bars", "klines", "candles")
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            bars = [item for item in candidate if isinstance(item, dict)]
            if bars:
                return bars
    return []


def coalesce(*values):
    return next((value for value in values if value is not None), None)


def js_string(value):
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, list):
        return ",".join("" if item is None else js_string(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    if type(value) is float and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    return js_json(value)
