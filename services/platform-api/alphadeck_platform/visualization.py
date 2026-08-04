import json
import re
from copy import deepcopy
from functools import lru_cache

from .config import ROOT
from .finance_values import js_string


@lru_cache(maxsize=1)
def catalogue():
    value = json.loads((ROOT / "config/visualization-catalog.json").read_text(encoding="utf-8"))
    if value.get("schemaVersion") != 1:
        raise ValueError("Unsupported visualization catalogue")
    return value


def normalize(value):
    return re.sub(r"\s+", "", value or "").lower()


def serialize_template(identity=None, context=None):
    context, data = context or {}, catalogue()
    templates = data["TEMPLATE_BY_CAPABILITY"]
    template = templates.get(identity or data["DEFAULT_TEMPLATE_ID"])
    if template is None:
        template = next(
            (value for value in templates.values() if value["id"] == identity),
            templates[data["DEFAULT_TEMPLATE_ID"]],
        )
    variants = data["VARIANTS_BY_TEMPLATE_ID"].get(template["id"])
    if variants is None:
        variants = [
            {
                "id": template["id"] + "-default",
                "name": template["name"],
                "scenario": template["scenario"],
                "layout": "default-data-dense-dashboard",
                "density": "dense",
                "match": {},
                "requiredComponents": template["requiredComponents"],
                "optionalComponents": template["optionalComponents"],
                "firstViewport": template["requiredComponents"][:3],
                "guidance": template["painPoints"],
            }
        ]
    instruction = normalize(context.get("instruction"))
    signals = set(map(normalize, context.get("dataSignals") or []))
    count = context.get("symbolCount")

    def score(variant):
        match = variant["match"]
        result = (
            1000
            if context.get("requestedVariantId") and context["requestedVariantId"] == variant["id"]
            else 0
        )
        if count is not None and count > 0:
            if ("minSymbols" in match and count < match["minSymbols"]) or (
                "maxSymbols" in match and count > match["maxSymbols"]
            ):
                return -1
            if "minSymbols" in match or "maxSymbols" in match:
                result += 3
        return (
            result
            + sum(4 for key in match.get("keywords", []) if normalize(key) in instruction)
            + sum(16 for key in match.get("strongKeywords", []) if normalize(key) in instruction)
            + sum(3 for signal in match.get("dataSignals", []) if normalize(signal) in signals)
        )

    eligible = [
        (score(variant), -index, variant)
        for index, variant in enumerate(variants)
        if score(variant) >= 0
    ]
    variant = max(eligible, key=lambda row: row[:2])[2] if eligible else variants[0]
    match, reasons = variant["match"], []
    if context.get("requestedVariantId") == variant["id"]:
        reasons.append("显式指定变体 " + variant["id"])
    if type(count) in {int, float}:
        reasons.append("识别到 " + js_string(count) + " 个标的")
    keywords = [
        key
        for key in [*match.get("strongKeywords", []), *match.get("keywords", [])]
        if normalize(key) in instruction
    ]
    if keywords:
        reasons.append("命中问题关键词：" + "、".join(keywords))
    matched_signals = [
        signal for signal in match.get("dataSignals", []) if normalize(signal) in signals
    ]
    if matched_signals:
        reasons.append("命中数据信号：" + "、".join(matched_signals))
    result = {
        "templateId": template["id"],
        "name": template["name"],
        "scenario": template["scenario"],
        "variantId": variant["id"],
        "variantName": variant["name"],
        "variantScenario": variant["scenario"],
        "layout": variant["layout"],
        "density": variant["density"],
        "matchReasons": reasons or ["使用该模板族的默认高质量页面变体"],
        "painPoints": template["painPoints"],
        "requiredComponents": variant["requiredComponents"],
        "optionalComponents": list(
            dict.fromkeys([*variant["optionalComponents"], *template["optionalComponents"]])
        ),
        "firstViewport": variant["firstViewport"],
        "variantGuidance": variant["guidance"],
        "dataSignals": list(
            dict.fromkeys([*template["dataSignals"], *match.get("dataSignals", [])])
        ),
        "finalDataContract": template["finalDataContract"],
        "alternatives": [
            {"variantId": item["id"], **{key: item[key] for key in ("name", "scenario", "layout")}}
            for item in variants
        ],
    }
    return deepcopy(result)


def visualization_contract(plan, symbol_count):
    config = plan.get("visualization") or {}
    template = serialize_template(
        plan.get("requestedCapabilityId") or plan.get("capabilityId"),
        {
            "instruction": plan.get("question"),
            "symbolCount": symbol_count,
            "requestedVariantId": config.get("variantId"),
            "dataSignals": config.get("dataSignals"),
        },
    )
    names = {
        "template_id": "templateId",
        "name": "name",
        "scenario": "scenario",
        "variant_id": "variantId",
        "variant_name": "variantName",
        "variant_scenario": "variantScenario",
        "layout": "layout",
        "density": "density",
        "first_viewport": "firstViewport",
        "variant_guidance": "variantGuidance",
        "match_reasons": "matchReasons",
        "alternatives": "alternatives",
        "pain_points": "painPoints",
        "required_components": "requiredComponents",
        "optional_components": "optionalComponents",
        "data_signals": "dataSignals",
        "final_data_contract": "finalDataContract",
    }
    return {
        **{key: template[value] for key, value in names.items()},
        "rendered_components": template["requiredComponents"],
        "missing_components": [],
    }
