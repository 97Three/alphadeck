from .dashboard_spec import assess_readiness, supported
from .data_identity import assess_identity, text
from .finance_values import coalesce, numeric, record
from .validation_inputs import read_json, read_plan


def nonempty(value):
    return isinstance(value, dict) and bool(value)


def usable_final(value):
    quote, kline = record(value.get("quote")) or {}, record(value.get("kline")) or {}
    root = text(value.get("symbol")) and (
        numeric(quote.get("price")) is not None
        or isinstance(kline.get("bars"), list)
        and bool(kline["bars"])
        or nonempty(value.get("financials"))
        or nonempty(value.get("backtest"))
    )
    assets = value.get("assets") if isinstance(value.get("assets"), list) else []
    asset_data = any(
        text(asset.get("symbol"))
        and (
            numeric((record(asset.get("quote")) or {}).get("price")) is not None
            or isinstance((record(asset.get("kline")) or {}).get("bars"), list)
            and bool(asset["kline"]["bars"])
        )
        for asset in assets
        if isinstance(asset, dict)
    )
    return bool(
        root
        or asset_data
        or value.get("status") == "no_candidates"
        and isinstance(value.get("assets"), list)
        and nonempty(value.get("screener"))
    )


def collected_symbols(value):
    result = set()

    def add(candidate):
        if normalized := text(candidate):
            result.add(normalized)

    add(value.get("symbol"))
    for key in ("requestedSymbols", "symbols"):
        for candidate in value.get(key, []) if isinstance(value.get(key), list) else []:
            add(candidate)
    for asset in value.get("assets", []) if isinstance(value.get("assets"), list) else []:
        if isinstance(asset, dict):
            add(asset.get("symbol"))
            add((record(asset.get("quote")) or {}).get("symbol"))
    return result


def assess_prepared(root, plan=None):
    plan = plan if plan is not None else read_plan(root)
    reasons = []
    result = {
        "ready": False,
        "reasons": reasons,
        "dashboardSpecReady": False,
        "dashboardSpecErrorCode": None,
        "dashboardSpecReasons": [],
    }
    if not plan or plan.get("status") != "planned":
        reasons.append("run_plan_not_planned")
        return result
    final, sources, quality = [
        record(read_json(root, relative))
        for relative in (
            "data_file/final/dashboard-data.json",
            "evidence/sources.json",
            "evidence/data_quality.json",
        )
    ]
    if final is None or not usable_final(final):
        reasons.append("final_data_not_usable")
    if (
        not sources
        or not isinstance(sources.get("sources"), list)
        or not any(
            any(text(entry.get(key)) for key in ("source", "endpoint", "dataset", "artifact_path"))
            for entry in sources["sources"]
            if isinstance(entry, dict)
        )
    ):
        reasons.append("sources_evidence_not_usable")
    if (
        not quality
        or quality.get("status") not in {"ok", "warning", "error"}
        or not any(
            isinstance(quality.get(key), list) and any(nonempty(entry) for entry in quality[key])
            for key in ("datasets", "checks")
        )
    ):
        reasons.append("quality_evidence_not_usable")
    visualization = record(plan.get("visualization")) or {}
    template, variant = text(visualization.get("templateId")), text(visualization.get("variantId"))
    if final is not None:
        reasons.extend(
            "dataset_identity:" + reason for reason in assess_identity(plan, final)["reasons"]
        )
        covered = collected_symbols(final)
        missing = [symbol for symbol in plan["symbols"] if symbol not in covered]
        if missing:
            reasons.append("missing_planned_symbols:" + ",".join(missing))
        final_template = text((record(final.get("visualization")) or {}).get("template_id"))
        if template:
            if not final_template:
                reasons.append("visualization_template_missing")
            elif final_template != template:
                reasons.append("visualization_template_mismatch")
    if final is not None and template and variant and supported(template, variant):
        readiness = assess_readiness(plan, final)
        result.update(
            dashboardSpecReady=readiness["ready"],
            dashboardSpecErrorCode=readiness["errorCode"],
            dashboardSpecReasons=readiness["reasons"],
        )
        if (
            not readiness["ready"]
            and readiness["errorCode"] == "DASHBOARD_SPEC_DATA_PREREQUISITE_FAILED"
        ):
            reasons.extend("dashboard_spec:" + reason for reason in readiness["reasons"])
    for label, evidence in (("sources", sources), ("quality", quality)):
        identity = text(coalesce((evidence or {}).get("runId"), (evidence or {}).get("run_id")))
        if not identity:
            reasons.append(label + "_evidence_run_id_missing")
        elif identity != plan["runId"]:
            reasons.append(label + "_evidence_run_id_mismatch")
    result["ready"] = not reasons
    return result
