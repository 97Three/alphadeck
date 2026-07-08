import re

from .finance_values import coalesce, record

DATASETS = (
    "quote",
    "kline",
    "technicalIndicators",
    "financials",
    "fundamentalIndicators",
    "announcements",
    "backtest",
)


def text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def symbol(value):
    value = text(value)
    return value if value and re.fullmatch(r"[6035][0-9]{5}", value) else None


def symbol_array(value):
    if not isinstance(value, list):
        return None
    values = [symbol(item) for item in value]
    return values if None not in values and len(set(values)) == len(values) else None


def assess_identity(plan_value, final_value):
    plan, final = record(plan_value), record(final_value)
    reasons = []
    run = text((plan or {}).get("runId"))
    final_run = text(coalesce((final or {}).get("runId"), (final or {}).get("run_id")))
    planned = symbol_array((plan or {}).get("symbols"))
    if plan is None:
        reasons.append("run_plan_invalid")
    if final is None:
        reasons.append("final_data_invalid")
    if not run:
        reasons.append("run_id_missing")
    if not final_run:
        reasons.append("final_run_id_missing")
    elif run and final_run != run:
        reasons.append("final_run_id_mismatch")
    if not planned:
        reasons.append("planned_symbols_invalid")
    if final is None or not planned:
        return {"ready": not reasons, "reasons": reasons, "runId": run, "symbols": planned or []}
    root = symbol(final.get("symbol"))
    if not root:
        reasons.append("final_root_symbol_missing")
    elif root != planned[0]:
        reasons.append("final_root_symbol_mismatch")
    for key, label in (("requestedSymbols", "requested"), ("symbols", "fetched")):
        values = symbol_array(final.get(key))
        if len(planned) > 1 and values is None:
            reasons.append("final_" + label + "_symbols_missing")
        elif values is not None and values != planned:
            reasons.append("final_" + label + "_symbols_mismatch")

    def datasets(value, identity, prefix=""):
        for key in DATASETS:
            nested = record(value.get(key))
            if not nested:
                continue
            code = symbol(nested.get("symbol"))
            if not code:
                reasons.append(prefix + key + "_symbol_missing")
            elif identity and code != identity:
                reasons.append(prefix + key + "_symbol_mismatch")

    datasets(final, root)
    assets = (
        [record(item) for item in final["assets"]]
        if isinstance(final.get("assets"), list)
        else None
    )
    if len(planned) > 1:
        codes = (
            symbol_array([item.get("symbol") for item in assets])
            if assets is not None and all(item is not None for item in assets)
            else None
        )
        if codes is None:
            reasons.append("asset_symbols_missing")
        elif codes != planned:
            reasons.append("asset_symbols_mismatch")
        for index, asset in enumerate(assets or []):
            if asset is not None:
                datasets(asset, symbol(asset.get("symbol")), f"assets[{index}].")
        for key in ("comparison", "selectionRanking"):
            rows = (record(final.get(key)) or {}).get("rows")
            codes = (
                symbol_array([(record(row) or {}).get("symbol") for row in rows])
                if isinstance(rows, list)
                else None
            )
            if codes and (len(codes) != len(planned) or any(code not in planned for code in codes)):
                reasons.append(key + "_symbols_mismatch")
    return {
        "ready": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "runId": run,
        "symbols": planned,
    }
