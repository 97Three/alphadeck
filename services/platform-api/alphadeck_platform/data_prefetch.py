from .baseline_evidence import FINAL, ensure_baseline
from .dashboard import comparison_summary, conclusion, selection_ranking, trading_plan
from .finance_values import bars_from_asset, coalesce, js_string, numeric, record
from .fundamentals import financial_quality, financial_quality_summary
from .image_evidence import augment_image_evidence, image_extraction
from .portfolio import correlation_summary, holding_rows, liquidity_summary, portfolio_summary
from .prefetch_market import screener_seed, symbol_dataset, timestamp
from .prefetch_planning import (
    broad_screener,
    is_quant_plan,
    planned_symbols,
    sync_plan_symbols,
    trading_plan_intent,
)
from .prefetch_transport import MarketDataError, collect_datasets
from .query_rewrite import rewrite_failure
from .research_time import research_time_for_plan
from .technical import ensure_technical_summary
from .visualization import visualization_contract


async def empty_screener_result(plan, screener, artifacts, raw_files, warnings):
    now, run = timestamp(), plan["runId"]
    requested, scanned = (
        numeric(screener.get("limit")) or 0,
        numeric(screener.get("scanned_symbols")) or 0,
    )
    warnings = list(
        dict.fromkeys(
            [
                *warnings,
                f"筛选器扫描 {js_string(scanned)} 个标的后未返回满足安全条件的候选；平台不会为凑足 {js_string(requested) if requested else '指定'} 只而放宽过滤或编造推荐。",
            ]
        )
    )
    raw_files.append(
        await artifacts.write(
            f"data_file/raw/{run}/batch-quotes.json",
            {
                "schemaVersion": 1,
                "status": "not_requested",
                "reason": "screener_returned_no_candidates",
                "requested_symbols": [],
                "quotes": [],
                "fetched_at": now,
                "source": "alphadeck-market-api",
            },
        )
    )
    comparison, quality = comparison_summary([]), financial_quality_summary([])
    selection = selection_ranking(comparison, [])
    data = {
        "schemaVersion": 1,
        "runId": run,
        "generatedAt": now,
        "status": "no_candidates",
        "result": "completed_without_candidates",
        "symbol": "A_SHARE_UNIVERSE",
        "name": "A 股短线候选池",
        "asset_type": "stock_selection",
        "source": js_string(coalesce(screener.get("source"), "alphadeck-market-api")),
        "as_of": coalesce(screener.get("trade_date"), screener.get("fetched_at"), now),
        "requestedSymbols": [],
        "symbols": [],
        "assetCount": 0,
        "assets": [],
        "comparison": comparison,
        "selectionRanking": selection,
        "financialQuality": quality,
        "correlation": correlation_summary([]),
        "liquidity": liquidity_summary([]),
        "tradingPlan": {
            **trading_plan([], selection),
            "status": "unavailable",
            "reason": "no_safe_candidates",
        },
        "screener": screener,
        "visualization": visualization_contract(plan, 0),
        "conclusion": {
            "summary": [
                f"本次共扫描 {js_string(scanned)} 个标的，未发现满足当前安全筛选条件的候选。",
                "平台没有为满足推荐数量而放宽涨跌停、停牌、流动性或数据完整性约束，也没有生成虚构股票。",
                "可在补齐目标交易日覆盖、放宽明确且可审计的研究条件后重新运行筛选。",
            ],
            "primary_view": "当前没有可安全列入研究优先级的候选，结果应视为“空候选”而不是生成失败。",
            "risk_disclaimer": "筛选结果仅用于研究，不构成投资建议、收益承诺或即时交易指令。",
        },
        "warnings": warnings,
    }
    await artifacts.write(FINAL, data)
    await ensure_baseline(artifacts, force=True)
    await artifacts.event(
        {
            "event_type": "data_prefetched",
            "stage": "data_collection",
            "status": "warning",
            "run_id": run,
            "artifact_path": FINAL,
            "summary": f"选股接口返回 0 个安全候选；已写入结构化空结果、证据文件和 {FINAL}。",
        }
    )
    return {
        "skipped": False,
        "symbols": [],
        "finalDataPath": FINAL,
        "rawFiles": raw_files,
        "summary": "选股已完成但没有满足安全条件的候选；已生成可验证的空结果看板。",
    }


async def prefetch_data(plan, transport, artifacts, *, on_progress=None, assert_active=None):
    async def active():
        if assert_active:
            await assert_active()

    await active()
    if plan.get("status") == "failed" or rewrite_failure(plan.get("queryRewrite")):
        return {"skipped": True, "summary": "研究规划失败，未执行数据预取。"}
    if plan.get("status") in {"needs_clarification", "refused"} or (
        plan.get("clarification") or {}
    ).get("required"):
        return {"skipped": True, "summary": "任务仍需用户补充关键信息，跳过平台预取数据。"}
    if not is_quant_plan(plan):
        return {
            "skipped": True,
            "summary": f"能力 {plan.get('capabilityId')} 暂不需要平台预取数据。",
        }
    time = research_time_for_plan(plan)
    run, raw_files, warnings, screener = plan["runId"], [], [], None
    symbols = planned_symbols(plan)
    if not symbols and broad_screener(plan):
        try:
            seed = await screener_seed(
                plan, transport, artifacts, raw_files, warnings, assert_active
            )
            symbols, screener = seed["symbols"], seed["screener"]
        except Exception as cause:
            await active()
            warnings.append("选股接口预取失败：" + str(cause))
    if not symbols and screener is not None:
        await sync_plan_symbols(artifacts, plan, symbols, "screener", warnings, timestamp())
        await artifacts.event(
            {
                "event_type": "data_prefetch_started",
                "stage": "data_collection",
                "status": "pending",
                "run_id": run,
                "summary": "平台已完成宽域 A 股筛选，正在固化零候选结果与证据。",
            }
        )
        return await empty_screener_result(plan, screener, artifacts, raw_files, warnings)
    if not symbols:
        return {
            "skipped": True,
            "summary": "未识别到 A 股、指数或 ETF 标的，跳过平台预取。"
            + (" " + "；".join(warnings) if warnings else ""),
        }
    await sync_plan_symbols(
        artifacts,
        plan,
        symbols,
        "screener" if screener is not None else "run_plan",
        warnings,
        timestamp(),
    )
    image = await image_extraction(artifacts, run, warnings)
    await artifacts.event(
        {
            "event_type": "data_prefetch_started",
            "stage": "data_collection",
            "status": "pending",
            "run_id": run,
            "summary": "平台开始预取 " + "、".join(symbols) + " 的真实行情数据。",
        }
    )
    quotes = {}
    if len(symbols) > 1 and time is None:
        try:
            batch = await transport.fetch(
                "/api/v1/quotes/realtime", method="POST", body={"symbols": symbols}
            )
            await active()
            quotes = {
                row["symbol"]: row
                for row in batch.get("quotes", [])
                if isinstance(row, dict) and isinstance(row.get("symbol"), str)
            }
            raw_files.append(await artifacts.write(f"data_file/raw/{run}/batch-quotes.json", batch))
        except Exception as cause:
            await active()
            warnings.append("批量实时行情预取失败，降级为逐只获取：" + str(cause))

    async def fetch(symbol, quote, paths, issues):
        return await symbol_dataset(
            symbol,
            plan,
            transport,
            artifacts,
            paths,
            issues,
            quote=quote,
            assert_active=assert_active,
        )

    collected = await collect_datasets(
        symbols, fetch, quotes=quotes, on_progress=on_progress, assert_active=assert_active
    )
    await active()
    raw_files.extend(collected["rawFiles"])
    warnings.extend(collected["warnings"])
    assets = collected["assets"]
    for asset in assets:
        if record(asset.get("technicalIndicators")) is not None or bars_from_asset(asset):
            ensure_technical_summary(asset)
        financial_quality(asset)
    if not assets:
        raise MarketDataError("所有标的预取失败：" + "；".join(warnings))
    primary = assets[0]
    comparison, quality = comparison_summary(assets), financial_quality_summary(assets)
    selection = selection_ranking(comparison, assets)
    data = {**primary, "runId": run}
    if len(symbols) > 1:
        data.update(
            schemaVersion=1,
            generatedAt=timestamp(),
            primarySymbol=primary.get("symbol"),
            requestedSymbols=symbols,
            symbols=[asset.get("symbol") for asset in assets],
            assetCount=len(assets),
            assets=assets,
            comparison=comparison,
            correlation=correlation_summary(assets),
        )
    if (
        plan.get("requestedCapabilityId") == "portfolio_risk"
        or plan.get("capabilityId") == "portfolio_risk"
    ):
        data.update(portfolio=portfolio_summary(assets), holdings=holding_rows(assets))
        if len(symbols) == 1:
            data.update(assets=assets, comparison=comparison_summary(assets))
    if image is not None and record(image.get("imageExtraction")) is not None:
        data["imageExtraction"] = image["imageExtraction"]
    if screener is not None:
        data["screener"] = screener
    data.update(
        visualization=visualization_contract(plan, len(assets) or len(symbols)),
        liquidity=liquidity_summary([primary] if len(symbols) == 1 else assets),
        financialQuality=quality,
        selectionRanking=selection,
        conclusion=conclusion(comparison, selection, quality),
        warnings=warnings,
    )
    if trading_plan_intent(plan["question"]):
        data["tradingPlan"] = trading_plan(assets, selection)
    await artifacts.write(FINAL, data)
    await ensure_baseline(artifacts, force=True)
    if image is not None:
        await augment_image_evidence(artifacts, image)
    listed = "、".join(js_string(asset.get("symbol")) for asset in assets)
    await artifacts.event(
        {
            "event_type": "data_prefetched",
            "stage": "data_collection",
            "status": "warning" if warnings else "success",
            "run_id": run,
            "artifact_path": FINAL,
            "summary": f"平台已预取 {listed} 数据：raw {len(raw_files)} 个文件，final 数据已写入。"
            + (" 警告：" + "；".join(warnings) if warnings else ""),
        }
    )
    return {
        "skipped": False,
        "symbol": js_string(coalesce(primary.get("symbol"), symbols[0])),
        "symbols": [
            js_string(coalesce(asset.get("symbol"), ""))
            for asset in assets
            if coalesce(asset.get("symbol"), "")
        ],
        "finalDataPath": FINAL,
        "rawFiles": raw_files,
        "summary": f"已预取 {listed} 真实数据并生成 {FINAL}。",
    }
