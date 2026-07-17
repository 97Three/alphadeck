import json
import math

from .file_tools import truncate
from .finance_values import record
from .inspector_source import bounded, strings
from .messages import js_length
from .provenance import js_json, object_keys


def parsed(file):
    if not file["exists"] or "content" not in file:
        return None, {}
    try:
        value = json.loads(file["content"])
        return True, record(value) or {}
    except ValueError:
        return False, {}


def array_length(value, key):
    value = (value or {}).get(key)
    return len(value) if isinstance(value, list) else 0


def records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def keys(value):
    return [key for raw in object_keys(value) if (key := bounded(raw, 80))][:32]


def summary_plan(file):
    valid, plan = parsed(file)
    visual = record(plan.get("visualization")) or {}
    return {
        "validJson": valid,
        "status": bounded(plan.get("status"), 48),
        "capabilityId": bounded(plan.get("capabilityId"), 80),
        "requestedCapabilityId": bounded(plan.get("requestedCapabilityId"), 80),
        "executionCapabilityId": bounded(plan.get("executionCapabilityId"), 80),
        "symbols": strings(plan.get("symbols"), 16, 32),
        "timeRange": bounded(plan.get("timeRange"), 100),
        "dataRequirements": strings(plan.get("dataRequirements"), 12, 120),
        "expectedArtifacts": strings(plan.get("expectedArtifacts"), 12, 160),
        "validationRuleCount": array_length(plan, "validationRules"),
        "visualization": {
            "required": visual.get("required") is True,
            "templateId": bounded(visual.get("templateId"), 100),
            "variantId": bounded(visual.get("variantId"), 100),
            "panels": strings(visual.get("panels"), 16, 100),
            "firstViewport": strings(visual.get("firstViewport"), 10, 100),
        },
    }


def summary_final(file):
    valid, value = parsed(file)
    visual = record(value.get("visualization")) or {}
    assets = records(value.get("assets"))
    primary = assets[0] if assets else {}

    def nested_length(value, key, child):
        return array_length(record(value.get(key)), child)

    return {
        "validJson": valid,
        "topLevelKeys": keys(value),
        "generatedAtPresent": isinstance(value.get("generatedAt"), str)
        or isinstance(value.get("generated_at"), str),
        "hasQuote": record(value.get("quote")) is not None
        or record(primary.get("quote")) is not None,
        "assetCount": len(assets),
        "barCount": max(
            nested_length(value, "kline", "bars"),
            nested_length(value, "history", "bars"),
            nested_length(primary, "kline", "bars"),
        ),
        "comparisonRowCount": array_length(record(value.get("comparison")), "rows"),
        "warningCount": array_length(value, "warnings"),
        "visualization": {
            "templateId": bounded(visual.get("template_id"), 100),
            "requiredComponentCount": array_length(visual, "required_components"),
            "renderedComponentCount": array_length(visual, "rendered_components"),
            "missingComponentCount": array_length(visual, "missing_components"),
        },
    }


def status_counts(items):
    result = {}
    for item in items[:200]:
        status = (bounded(item.get("status"), 32) or "unknown").lower()
        result[status] = result.get(status, 0) + 1
    return result


def summary_sources(file):
    valid, value = parsed(file)
    sources = records(value.get("sources"))
    return {
        "validJson": valid,
        "topLevelKeys": keys(value),
        "sourceCount": len(sources),
        "sampledStatusCounts": status_counts(sources),
        "datasets": [
            dataset
            for source in sources
            if (dataset := bounded(source.get("dataset"), 100)) is not None
        ][:16],
    }


def summary_quality(file):
    valid, value = parsed(file)
    datasets = records(value.get("datasets"))
    return {
        "validJson": valid,
        "topLevelKeys": keys(value),
        "status": bounded(value.get("status"), 32),
        "datasetCount": len(datasets),
        "checkCount": array_length(value, "checks"),
        "warningCount": array_length(value, "warnings"),
        "limitationCount": array_length(value, "limitations"),
        "missingFieldCount": sum(array_length(item, "missing_fields") for item in datasets),
        "sampledStatusCounts": status_counts(datasets),
    }


def select(value, fields):
    return {key: value[key] for key in fields if isinstance(value, dict) and key in value}


def numbers(value, maximum=8):
    return (
        [
            item
            for item in value
            if type(item) in {int, float}
            and math.isfinite(item)
            and 0 < item <= 9007199254740991
            and item == int(item)
        ][:maximum]
        if isinstance(value, list)
        else []
    )


def compact_page(value):
    if not isinstance(value, dict):
        return None
    binding, chart = record(value.get("dataBinding")), record(value.get("chart"))
    return {
        "clientComponent": value.get("clientComponent") is True,
        "dataBinding": {
            "standardFinalDataPath": binding.get("standardFinalDataPath") is True,
            "standardFinalDataPathLines": numbers(binding.get("standardFinalDataPathLines"), 3),
            "serverFileReadLines": numbers(binding.get("serverFileReadLines"), 4),
            "sameOriginMarketApi": binding.get("sameOriginMarketApi") is True,
            "sameOriginMarketApiLines": numbers(binding.get("sameOriginMarketApiLines"), 4),
        }
        if binding is not None
        else None,
        "chart": {
            "hasChartSignal": chart.get("hasChartSignal") is True,
            "markupOrLibraryLines": numbers(chart.get("markupOrLibraryLines"), 6),
            "dataDerivationLines": numbers(chart.get("dataDerivationLines"), 6),
        }
        if chart is not None
        else None,
        "evidenceDisplayLines": numbers(value.get("evidenceDisplayLines"), 5),
    }


def compact_ui(value):
    if not isinstance(value, dict):
        return None
    styles = record(value.get("styles"))
    return {
        "root": value.get("root"),
        "regions": records(value.get("regions"))[:12],
        "components": [
            {
                **select(component, ["name", "range", "renderLines"]),
                "classNames": strings(component.get("classNames"), 5, 48),
            }
            for component in records(value.get("components"))[:14]
        ],
        "styles": {
            "usedClassCount": styles.get("usedClassCount"),
            "inspectedRuleCount": styles.get("inspectedRuleCount"),
            "layoutRules": records(styles.get("layoutRules"))[:8],
            "cardSurfaceCandidates": records(styles.get("cardSurfaceCandidates"))[:8],
        }
        if styles is not None
        else None,
        "preparedVisualEdit": record(value.get("preparedVisualEdit")),
    }


def compact_report(report):
    files, plan, artifacts = (
        report.get("files") or {},
        record(report.get("runPlan")),
        report.get("artifacts") or {},
    )
    final, sources, quality = (
        artifacts.get("finalData"),
        artifacts.get("sources"),
        artifacts.get("dataQuality"),
    )
    return {
        "schemaVersion": 1,
        "advisory": report["advisory"],
        "reportTruncated": True,
        "files": select(files, ["page", "styles", "marketProxy"]),
        "requiredArtifacts": {
            key: (record(files.get(key)) or {}).get("exists", False)
            for key in ("runPlan", "finalData", "sources", "dataQuality")
        },
        "missingRequiredFiles": report["missingRequiredFiles"],
        "task": select(
            plan,
            [
                "status",
                "capabilityId",
                "requestedCapabilityId",
                "symbols",
                "timeRange",
                "visualization",
            ],
        )
        if plan is not None
        else None,
        "dataHealth": {
            "finalData": select(
                final,
                [
                    "validJson",
                    "generatedAtPresent",
                    "hasQuote",
                    "assetCount",
                    "barCount",
                    "comparisonRowCount",
                    "warningCount",
                    "visualization",
                ],
            ),
            "sources": select(sources, ["validJson", "sourceCount", "sampledStatusCounts"]),
            "dataQuality": select(
                quality,
                [
                    "validJson",
                    "status",
                    "datasetCount",
                    "checkCount",
                    "warningCount",
                    "limitationCount",
                    "missingFieldCount",
                ],
            ),
        },
        "pageContract": compact_page(report.get("pageContract")),
        "marketProxy": report.get("marketProxy"),
        "responsive": report.get("responsive"),
        "uiInspection": compact_ui(report.get("uiInspection")),
        "nextActions": report.get("nextActions"),
    }


def minimal_report(report, next_action):
    compact = compact_report(report)
    ui, task = compact["uiInspection"], compact["task"]
    styles = (ui or {}).get("styles")
    prepared = (ui or {}).get("preparedVisualEdit")
    return {
        "schemaVersion": 1,
        "advisory": report["advisory"],
        "reportTruncated": True,
        "files": compact["files"],
        "missingRequiredFiles": report["missingRequiredFiles"],
        "task": {
            **select(task, ["capabilityId", "symbols"]),
            "visualization": select(task.get("visualization"), ["templateId", "variantId"])
            if isinstance(task.get("visualization"), dict)
            else None,
        }
        if task is not None
        else None,
        "pageContract": compact["pageContract"],
        "uiInspection": {
            "root": ui.get("root"),
            "regions": records(ui.get("regions"))[:10],
            "components": [
                {
                    **select(component, ["name", "range", "renderLines"]),
                    **(
                        {"classNames": strings(component.get("classNames"), 5, 48)}
                        if index < 3
                        else {}
                    ),
                }
                for index, component in enumerate(records(ui.get("components"))[:10])
            ],
            "styles": {
                "usedClassCount": styles.get("usedClassCount"),
                "layoutRules": records(styles.get("layoutRules"))[:6],
                "cardSurfaceCandidates": [
                    select(item, ["selector", "range"])
                    for item in records(styles.get("cardSurfaceCandidates"))[:8]
                ],
            }
            if styles is not None
            else None,
            "preparedVisualEdit": select(
                prepared,
                ["status", "objective", "pageMarker", "cssRecipe", "minimalReadsBeforeEdit"],
            )
            if prepared is not None
            else None,
        }
        if ui is not None
        else None,
        "nextAction": next_action,
    }


def render_report(report, maximum, next_action):
    full = js_json(report, indent=2)
    length = js_length(full)
    if length <= maximum:
        return {"content": full, "truncated": False, "originalChars": length}
    compact = js_json(compact_report(report))
    if js_length(compact) <= maximum:
        return {"content": compact, "truncated": True, "originalChars": length}
    minimal = js_json(minimal_report(report, next_action))
    return {
        "content": minimal if js_length(minimal) <= maximum else truncate(minimal, maximum)["text"],
        "truncated": True,
        "originalChars": length,
    }
