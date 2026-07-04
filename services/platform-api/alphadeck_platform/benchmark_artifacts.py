import re
from pathlib import Path

from .evaluation_diagnostics import inspect_backtest
from .evaluation_oracles import evaluate
from .evaluation_reports import records
from .evaluation_scoring import record
from .finance_values import js_string
from .scaffold import controlled_path
from .skill_integrity import read_regular
from .skill_source import strict_json


def event_audit(root, case, *, expect_final=True):
    try:
        lines = (
            read_regular(controlled_path(root, ".data-agent/events.jsonl"), 16777216)
            .decode()
            .splitlines()
        )
    except OSError:
        lines = []
    events = []
    for line in lines:
        try:
            value = strict_json(line)
            if isinstance(value, dict):
                events.append(value)
        except ValueError:
            continue
    kinds = list(dict.fromkeys(event.get("event_type") for event in events))
    required = (
        [
            "data_prefetch_started",
            "data_prefetched",
            "data_quality_checked",
            "validation_started",
            "validation_completed",
        ]
        if expect_final
        else []
    )
    if case.get("expectedImageExtraction"):
        required.append("image_attachment_evidence_created")
    if case.get("expectClarification"):
        required.append("intent_clarification_required")
    failures = (
        ["events.jsonl 缺少规划事件。"]
        if not any(kind in kinds for kind in ("run_planned", "intent_clarification_required"))
        else []
    )
    failures.extend("events.jsonl 缺少 " + kind + "。" for kind in required if kind not in kinds)
    return {
        "failures": failures,
        "total": len(events),
        "eventTypes": kinds,
        "stages": list(dict.fromkeys(event["stage"] for event in events if event.get("stage"))),
        "warningCount": sum(event.get("status") == "warning" for event in events),
        "errorCount": sum(event.get("status") == "error" for event in events),
    }


async def inspect_artifacts(root, case, prefetch):
    failures, missing, invalid = [], [], []

    async def read(relative, *, text=False):
        try:
            content = read_regular(controlled_path(root, relative), 33554432).decode(
                "utf-8", "replace"
            )
        except OSError:
            missing.append(relative)
            failures.append("缺少产物 " + relative)
            return None
        try:
            return content if text else strict_json(content)
        except ValueError:
            invalid.append(relative)
            failures.append("产物 JSON 无效：" + relative)
            return None

    final, quality, sources, page, plan = [
        await read(relative, text=relative.endswith((".tsx", ".vue")))
        for relative in (
            "data_file/final/dashboard-data.json",
            "evidence/data_quality.json",
            "evidence/sources.json",
            "src/App.vue",
            ".data-agent/finance-run-plan.json",
        )
    ]
    if any(item is None for item in (final, quality, sources, page, plan)) or not page:
        return {
            "status": "failed",
            "failures": failures,
            "missingArtifacts": missing,
            "invalidArtifacts": invalid,
            "finalData": None,
            "quality": None,
            "oracle": {
                "passed": False,
                "warning": False,
                "checks": [],
                "failures": ["关键产物缺失，无法执行事实 oracle。"],
                "warnings": [],
            },
        }
    final, quality, sources, plan = map(record, (final, quality, sources, plan))

    def check(condition, message):
        if not condition:
            failures.append(message)

    for key, expected in (("symbol", "expectedSymbol"), ("asset_type", "expectedAssetType")):
        if case.get(expected):
            check(
                final.get(key) == case[expected],
                f"{key} 应为 {case[expected]}，实际为 {final.get(key)}",
            )
    check(
        quality.get("status") in ("ok", "warning"),
        "evidence/data_quality.json 状态应为 ok/warning，实际为 " + str(quality.get("status")),
    )
    check(
        isinstance(sources.get("sources"), list) and bool(sources["sources"]),
        "evidence/sources.json 应包含 sources。",
    )
    for marker, message in (
        ("data_file/final/dashboard-data.json", "src/App.vue 应读取 final 数据文件。"),
        ("/api/market", "src/App.vue 应声明 /api/market 数据入口。"),
        ("<svg", "src/App.vue 应包含 SVG 图表实现。"),
    ):
        check(marker in page, message)
    visual = record(final.get("visualization"))
    template = (
        case.get("expectedTemplateId") or visual.get("template_id") or visual.get("templateId")
    )
    if not template or template in (
        "single-stock-diagnosis",
        "technical-timing",
        "fundamental-research",
        "backtest-review",
    ):
        check("K 线与量价结构" in page, "src/App.vue 应包含 K 线与量价结构面板。")
        check(
            "candle-up" in page and "candle-down" in page,
            "src/App.vue 应实现涨跌 K 线/OHLC 结构。",
        )
        check("volume-chart" in page, "src/App.vue 应包含成交量副图。")
        check("量化信号" in page or "信号摘要" in page, "src/App.vue 应包含量化信号摘要面板。")
    check(
        any(
            marker in page
            for marker in ("data_quality", "DataQualityPanel", "数据质量", "更新时间", "数据缺口")
        ),
        "src/App.vue 应展示数据质量或限制信息。",
    )
    for key in case.get("expectedFinalFields", []):
        check(key in final, "final 数据缺少字段 " + key)
    for expected, planned, raw in (
        ("expectedTemplateId", "templateId", "template_id"),
        ("expectedVariantId", "variantId", "variant_id"),
    ):
        if case.get(expected):
            check(
                record(plan.get("visualization")).get(planned) == case[expected],
                f"run_plan.visualization.{planned} 应为 {case[expected]}，实际为 {record(plan.get('visualization')).get(planned)}",
            )
            check(
                (visual.get(raw) or visual.get(planned)) == case[expected],
                f"final visualization.{raw} 应为 {case[expected]}，实际为 {visual.get(raw) or visual.get(planned)}",
            )
    expected_symbols = case.get("expectedSymbols", [])
    if expected_symbols:
        requested = set(
            map(
                js_string,
                [
                    *final.get("requestedSymbols", []),
                    *final.get("symbols", []),
                    final.get("symbol"),
                ],
            )
        )
        assets = {
            str(item.get("symbol") or record(item.get("quote")).get("symbol") or "")
            for item in records(final.get("assets"))
        }
        comparisons = {
            str(item.get("symbol") or "")
            for item in records(record(final.get("comparison")).get("rows"))
        }
        for values, label in (
            (requested, "requestedSymbols/symbols"),
            (assets, "assets[]"),
            (comparisons, "comparison.rows[]"),
        ):
            absent = [symbol for symbol in expected_symbols if symbol not in values]
            check(not absent, label + " 缺少：" + "、".join(absent))
        check(
            len(records(final.get("assets"))) >= len(expected_symbols),
            f"assets[] 数量应至少为 {len(expected_symbols)}，实际为 {len(records(final.get('assets')))}",
        )
    expected_fields = case.get("expectedFinalFields", [])
    if "selectionRanking" in expected_fields:
        screener = record(final.get("screener"))
        empty = (
            final.get("status") == "no_candidates"
            and screener.get("total_candidates") == 0
            and screener.get("candidates") == []
            and final.get("assets") == []
        )
        for key in ("selectionRanking", "financialQuality"):
            rows = record(final.get(key)).get("rows")
            check(
                isinstance(rows, list) and (empty or bool(rows)),
                "空候选结果仍应包含 " + key + ".rows 数组。" if empty else key + ".rows 应非空。",
            )
        rows = record(final.get("comparison")).get("rows")
        if empty:
            check(isinstance(rows, list), "空候选结果仍应包含 comparison.rows 数组。")
            check(
                record(final.get("tradingPlan")).get("status") == "unavailable",
                "空候选结果的 tradingPlan.status 应为 unavailable。",
            )
            check(
                isinstance(final.get("warnings"), list) and bool(final["warnings"]),
                "空候选结果应包含可读的 warnings。",
            )
            check(
                re.search("noCandidates|没有满足安全条件的候选|结构化空结果|暂无候选", page),
                "空候选页面应明确展示无安全候选状态。",
            )
        else:
            check(
                isinstance(rows, list)
                and all(
                    isinstance(row, dict) and "composite_score" in row and row.get("selection_view")
                    for row in rows
                ),
                "comparison.rows[] 应包含 composite_score 和 selection_view。",
            )
        check(
            re.search(
                "selectionRanking|financialQuality|stock-selection|相对强弱|财务质量|收益对比|回撤对比",
                page,
                re.I,
            ),
            "选股页面应包含排名、财务质量或对比图表组件。",
        )
    if "portfolio" in expected_fields:
        check(isinstance(final.get("portfolio"), dict), "portfolio 应为对象。")
        check(bool(records(final.get("holdings"))), "holdings[] 应非空。")
        check(
            isinstance(record(final.get("portfolio")).get("concentration"), dict),
            "portfolio.concentration 应存在。",
        )
        check(
            re.search("holding-analysis|持仓|仓位|集中度|调仓|相关性|流动性", page),
            "持仓页面应包含持仓、仓位集中度、调仓或风险组件。",
        )
    if "backtest" in expected_fields:
        check(
            bool(records(record(final.get("backtest")).get("equity_curve"))),
            "backtest 应包含 equity_curve。",
        )
        check(
            isinstance(record(final.get("backtest")).get("summary"), dict),
            "backtest 应包含 summary。",
        )
        check("回测与风险" in page or "回测结果" in page, "src/App.vue 应包含回测面板。")
    raw_files = prefetch.get("rawFiles", [])
    raw_names = {Path(path).name for path in raw_files}
    for expected in case.get("expectedRawFiles", []):
        if expected == "backtest-ma-crossover.json":
            failures.extend(await inspect_backtest(final.get("backtest"), raw_files, read))
        else:
            check(expected in raw_names, "raw 数据缺少 " + expected)
    datasets = {item.get("id") for item in records(quality.get("datasets"))}
    for expected in case.get("expectedDatasets", []):
        check(expected in datasets, "data_quality 缺少数据集 " + expected)
    for path in case.get("expectedEvidenceFiles", []):
        check(controlled_path(root, path).is_file(), "缺少 evidence 文件 " + path)
    if case.get("expectedImageExtraction"):
        image = record(await read("evidence/image_extraction.json"))
        check(isinstance(final.get("imageExtraction"), dict), "final 数据缺少 imageExtraction。")
        check(
            image.get("status") == "metadata_ready",
            f"image_extraction.status 应为 metadata_ready，实际为 {image.get('status')}",
        )
        check(bool(records(image.get("images"))), "image_extraction.images 应非空。")
        check(
            record(final.get("imageExtraction")).get("needs_manual_confirmation") is True,
            "imageExtraction.needs_manual_confirmation 应为 true。",
        )
        check(
            "uploaded_image_attachment" in datasets,
            "data_quality 缺少 uploaded_image_attachment 数据集。",
        )
        check(
            any(
                item.get("id") == "uploaded_image_attachment"
                for item in records(sources.get("sources"))
            ),
            "sources 缺少 uploaded_image_attachment 信源。",
        )
    if case.get("expectedQualityStatus"):
        check(
            quality.get("status") == case["expectedQualityStatus"],
            f"data_quality.status 应为 {case['expectedQualityStatus']}，实际为 {quality.get('status')}",
        )
    for provider in case.get("expectedSourceProviders", []):
        check(
            any(
                re.search(provider, str(item.get("source") or ""), re.I)
                for item in records(sources.get("sources"))
            ),
            "sources 应包含信源 " + provider,
        )
    if case.get("expectedNoCardSurface"):
        visual_report = record(await read(".data-agent/visual-validation.json"))
        viewports = records(visual_report.get("viewports"))
        check(len(viewports) >= 2, "无卡片定制必须保留桌面端和移动端视觉证据。")
        for viewport in viewports:
            metrics, identity = record(viewport.get("metrics")), viewport.get("id") or "unknown"
            check(
                metrics.get("hasFinancialWorkbenchMarker") is True,
                identity + " 无卡片定制缺少 financial-workbench 标记。",
            )
            check(metrics.get("cardGridClusterCount") == 0, identity + " 仍存在独立圆角卡片网格。")
            check(
                (metrics.get("firstViewportCardLikeSurfaceCount") or 0) < 4
                and (metrics.get("cardLikeSurfaceCount") or 0) < 8,
                identity + " 独立卡片式容器仍然偏多。",
            )
    oracle = evaluate(
        case.get("oracleAssertions", []),
        {"finalData": final, "sources": sources, "quality": quality, "page": page},
    )
    failures.extend(oracle["failures"])
    summary = {key: final[key] for key in ("symbol", "name", "asset_type") if key in final}
    for key, container, array in (
        ("klineRows", "kline", "bars"),
        ("reportRows", "financials", "reports"),
        ("announcementRows", "announcements", "announcements"),
        ("backtestRows", "backtest", "equity_curve"),
        ("tradeRows", "backtest", "trades"),
        ("comparisonRows", "comparison", "rows"),
    ):
        summary[key] = len(record(final.get(container)).get(array) or [])
    summary.update(
        assetCount=len(final.get("assets") or []),
        holdingCount=len(final.get("holdings") or []),
        hasImageExtraction=bool(final.get("imageExtraction")),
    )
    if visual.get("template_id") or visual.get("templateId"):
        summary["templateId"] = visual.get("template_id") or visual["templateId"]
    return {
        "status": "failed" if failures else "passed",
        "failures": failures,
        "missingArtifacts": missing,
        "invalidArtifacts": invalid,
        "oracle": oracle,
        "finalData": summary,
        "quality": {
            "status": quality.get("status"),
            "datasets": [
                {key: item[key] for key in ("id", "status", "row_count") if key in item}
                for item in records(quality.get("datasets"))
            ],
        },
    }
