import re
from copy import deepcopy

from .finance_catalog import FinanceCatalog
from .finance_intent import CATALOG, assess_intent
from .llm_config import project_llm_config
from .prefetch_market import timestamp
from .prefetch_planning import unique_symbols
from .query_rewrite import rewrite_failure, rewrite_query
from .research_time import research_time_for_plan, resolve_research_time
from .visualization import serialize_template


def strip_operational(instruction):
    value = instruction.strip()
    for marker in CATALOG["operationalMarkers"]:
        index = value.find(marker)
        if index > 0:
            value = value[:index].strip()
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"\n*Image #[0-9]+ path: [^\n]+", "", value)).strip()


def compact(instruction):
    return re.sub(r"\s+", "", strip_operational(instruction))


def portfolio_intent(instruction, image=False):
    return bool(
        re.search(
            "持仓|仓位|组合持仓|我的组合|账户组合|持仓组合|调仓|盈亏|成本价|持仓成本|买入成本|成本线|成本偏离|账户|证券账户|总资产|可用资金|可用现金|浮动盈亏|持仓截图|截图持仓|账户截图|交易截图|交割单",
            compact(instruction),
        )
        or image is True
        and re.search(
            "持仓|仓位|账户|证券|交易|盈亏|成本价|持仓成本|买入成本|现金|总资产|调仓",
            compact(instruction),
        )
    )


def dashboard_revision(instruction):
    value = compact(instruction)
    return bool(
        re.search(
            "这个|当前|现在|刚才|上一轮|上一版|原页面|结果|看板|页面|图表|方向对|不够贴题|重构|优化|调整|修改|改成|删除|新增|补充|保留|替换|不要|必须|移动端|横向溢出|折线图|热力图|矩阵",
            value,
        )
        and re.search(
            "重构|优化|调整|修改|改成|删除|新增|补充|保留|替换|不要|必须|方向对|不够贴题|移动端|横向溢出|折线图|热力图|矩阵",
            value,
        )
    )


def broad_instruction(instruction):
    value = re.sub(r"\s+", "", instruction)
    return bool(
        re.search("股票|个股|A股|全A|股票池", value)
        and re.search(
            r"全A|A股股票池|股票池|选股|筛选|候选|短线候选|次日|明日|明天|今日|今天|要买|买股|买入策略|短线|推荐[0-9]*(?:只|个)?(?:股票|个股)|(?:股票|个股).{0,12}推荐|推荐.{0,18}(?:股票|个股)",
            value,
        )
    )


def inherit_previous(instruction, explicit, previous, image=False):
    return bool(
        previous
        and previous.get("status") == "planned"
        and not rewrite_failure(previous.get("queryRewrite"))
        and not explicit
        and unique_symbols(
            [item.strip() for item in previous.get("symbols", []) if isinstance(item, str)]
        )
        and dashboard_revision(instruction)
        and not (
            portfolio_intent(instruction, image)
            and previous.get("capabilityId") != "portfolio_risk"
        )
    )


def infer_capability(requested, source, profile, rewrite, count):
    if requested and source in {"manual", "benchmark"}:
        return requested
    if (
        not requested
        and profile.get("selectedCapabilityId")
        and profile.get("selectionSource") == "manual"
    ):
        return profile["selectedCapabilityId"]
    if rewrite["analysisFocus"]["id"] == "portfolio_risk":
        return "portfolio_risk"
    if rewrite["broadUniverse"] or rewrite["analysisFocus"]["id"] == "comparison" and count >= 2:
        return "asset_comparison"
    return (
        rewrite.get("capabilityHint")
        or requested
        or ("asset_comparison" if count >= 2 else profile.get("selectedCapabilityId"))
    )


def merge_clarification(base, rewrite, capability, image=False):
    if image or rewrite["broadUniverse"]:
        return base
    actionable = [
        issue
        for issue in rewrite["issues"]
        if issue["code"] in {"TARGET_NOT_FOUND", "TARGET_AMBIGUOUS"}
    ]
    if not actionable:
        return base
    comparison = (
        capability in {"asset_comparison", "portfolio_risk"}
        or rewrite["analysisFocus"]["id"] == "comparison"
        or bool(rewrite["resolvedSymbols"])
    )
    questions = []
    for issue in actionable:
        target = issue.get("target", "undefined")
        if issue["code"] == "TARGET_AMBIGUOUS":
            ambiguity = next(
                (item for item in rewrite["ambiguousTargets"] if item["query"] == target), {}
            )
            candidates = "、".join(
                item["name"]
                + "（"
                + item["symbol"]
                + ("." + item["market"] if item.get("market") else "")
                + "）"
                for item in ambiguity.get("candidates", [])
            )
            questions.append(
                f"“{target}”对应多个证券：{candidates}。你想分析哪一个？"
                if candidates
                else f"“{target}”对应多个证券，请补充市场或代码。"
            )
        else:
            questions.append(f"没有找到“{target}”对应的证券，请确认名称或提供六位代码。")
    return {
        "required": True,
        "reason": "；".join(issue["message"] for issue in actionable),
        "missing": list(
            dict.fromkeys([*base["missing"], "comparison_universe" if comparison else "target"])
        ),
        "questions": list(dict.fromkeys([*questions, *base["questions"]]))[:3],
        "confidence": min(base["confidence"], rewrite["confidence"]),
        **({"defaults": base["defaults"]} if "defaults" in base else {}),
    }


def planned_notice(requested, execution):
    return (
        []
        if requested == execution or requested == "asset_comparison"
        else [
            f"用户选择的能力为 {requested}，当前先映射到已验证执行链路 {execution}。",
            "页面必须显式说明尚未完全接入的分析维度，避免把计划能力包装成已完成结果。",
        ]
    )


def build_plan(params, rewrite, *, profile=None, now=None, catalog=None):
    catalog, profile = catalog or FinanceCatalog(), profile or {}
    instruction = strip_operational(params["instruction"]) or params["instruction"].strip()
    explicit = list(dict.fromkeys(item["symbol"] for item in rewrite["resolvedSymbols"]))
    previous, image = params.get("previousPlan"), params.get("hasImageAttachments")
    inherit = inherit_previous(instruction, explicit, previous, image)
    inherited = previous.get("capabilityId") if inherit else None
    requested = params.get("capabilityId") or inherited
    source = (
        params.get("capabilitySource")
        if params.get("capabilityId")
        else "manual"
        if inherited
        else params.get("capabilitySource")
    )
    identity = infer_capability(requested, source, profile, rewrite, len(explicit))
    capability = catalog.capability(identity)
    execution = (
        capability
        if capability["id"] == "asset_comparison" or capability["status"] == "ready"
        else catalog.capability(capability["executionCapabilityId"])
    )
    settings = catalog.quant_settings(capability["id"])
    now = now or timestamp()
    symbols = explicit or (
        unique_symbols(
            [item.strip() for item in previous.get("symbols", []) if isinstance(item, str)]
        )
        if inherit
        else []
    )
    requested_time = (rewrite.get("timeRange") or {}).get("label")
    if requested_time is None and inherit:
        requested_time = previous.get("timeRange")
    clarification = merge_clarification(
        assess_intent(
            {
                "instruction": instruction,
                "capabilityId": capability["id"],
                "symbols": symbols,
                "timeRange": requested_time,
                "hasImageAttachments": image,
                "semanticFocusId": rewrite["analysisFocus"]["id"],
                "broadUniverse": rewrite["broadUniverse"],
            }
        ),
        rewrite,
        capability["id"],
        image,
    )
    refused, failure = rewrite["safety"]["decision"] == "refuse", rewrite_failure(rewrite)
    time_range = requested_time
    if not clarification["required"] and not refused and not failure and requested_time is None:
        time_range = (
            "最近报告期"
            if capability["id"] == "fundamental_analysis"
            else "最近 120 个交易日"
            if capability["id"]
            in {
                "stock_diagnosis",
                "technical_analysis",
                "asset_comparison",
                "sector_rotation",
                "strategy_research",
                "backtest_review",
            }
            else None
        )
    notice = planned_notice(capability["id"], execution["id"])
    rules = list(
        dict.fromkeys([*capability["validationRules"], *notice, *settings["validationRules"]])
    )
    reselect = re.search(
        "相关性|热力图|分散|流动性|成交额|换手|强弱|累计收益|收益曲线|净值曲线|折线图|排名|排序|候选|选股",
        compact(instruction),
    )
    context = {
        "instruction": instruction,
        "requestedVariantId": (previous.get("visualization") or {}).get("variantId")
        if inherit and not reselect
        else None,
    }
    if symbols:
        context["symbolCount"] = len(symbols)
    template = serialize_template(capability["id"], context)
    visual = {
        key: template[key]
        for key in (
            "templateId",
            "name",
            "scenario",
            "variantId",
            "variantName",
            "variantScenario",
            "layout",
            "density",
            "firstViewport",
            "variantGuidance",
            "matchReasons",
            "painPoints",
            "dataSignals",
            "finalDataContract",
        )
    }
    visual.update(
        required=not refused
        and not failure
        and not clarification["required"]
        and rewrite["outputIntent"] == "dashboard",
        panels=template["requiredComponents"],
        optionalPanels=template["optionalComponents"],
    )
    variant = "resolved" if symbols else "broad" if broad_instruction(instruction) else "ordinary"
    steps = (
        [failure["message"]]
        if failure
        else ["停止执行取数和生成任务，返回确定性安全说明。"]
        if refused
        else [
            *notice,
            "补充用户缺失的关键输入。",
            "确认标的、对比范围或投资约束后，再重新生成 planned 状态的执行计划。",
        ]
        if clarification["required"]
        else [*notice, *CATALOG["analysisSteps"][capability["id"]][variant]]
    )
    research_time = (
        None
        if failure or refused
        else resolve_research_time(rewrite["timeRange"])
        if rewrite.get("timeRange")
        else research_time_for_plan(previous)
        if inherit
        else None
    )
    plan = {
        "schemaVersion": 1,
        "runId": params["requestId"],
        "status": "refused"
        if refused
        else "failed"
        if failure
        else "needs_clarification"
        if clarification["required"]
        else "planned",
        "capabilityId": capability["id"],
        "composition": catalog.resolve(capability_id=capability["id"])["composition"],
        "llm": project_llm_config(params.get("llmModel")),
        "requestedCapabilityId": capability["id"],
        "executionCapabilityId": execution["id"],
        "question": instruction,
        "queryRewrite": rewrite,
        "symbols": symbols,
        "timeRange": time_range,
        "researchTime": research_time,
        "dataRequirements": []
        if failure
        else list(dict.fromkeys([*execution["dataEndpoints"], *settings["dataEndpoints"]])),
        "analysisSteps": steps,
        "visualization": visual,
        "expectedArtifacts": [".data-agent/finance-run-plan.json", ".data-agent/events.jsonl"]
        if clarification["required"] or refused or failure
        else list(
            dict.fromkeys([*capability["expectedArtifacts"], *settings["expectedArtifacts"]])
        ),
        "validationRules": rules,
        "createdAt": now,
        "updatedAt": now,
    }
    if not refused and not failure and clarification["required"]:
        plan["clarification"] = clarification
    if failure:
        plan["failure"] = failure
    if refused and rewrite["safety"].get("code") and rewrite["safety"].get("message"):
        plan["refusal"] = {key: rewrite["safety"][key] for key in ("code", "message")}
    return plan


def task_projection(rewrite):
    return {
        "schemaVersion": 1,
        "originalQuery": rewrite["originalQuery"],
        "objective": rewrite["rewrittenQuery"],
        "entities": [{"text": text, "evidence": text} for text in rewrite["targetCandidates"]],
        "resolvedEntities": [
            {
                "mention": entity["query"],
                "entityType": "finance.security",
                "canonicalId": entity["symbol"],
                "displayName": entity["name"],
                "attributes": {
                    key: entity[source]
                    for key, source in (
                        ("market", "market"),
                        ("assetType", "assetType"),
                        ("secid", "secid"),
                    )
                    if source in entity
                },
                "resolverId": "finance.security-resolver",
                "confidence": entity["confidence"],
            }
            for entity in rewrite["resolvedSymbols"]
        ],
        "metrics": [
            {"id": rewrite["analysisFocus"]["id"], "name": rewrite["analysisFocus"]["label"]}
        ],
        "dimensions": [],
        "filters": [],
        "timeRange": {
            "label": rewrite["timeRange"]["label"],
            "granularity": rewrite["timeRange"]["unit"],
        }
        if rewrite.get("timeRange")
        else None,
        "output": rewrite["outputIntent"],
        "domainHints": ["finance.quant", rewrite.get("capabilityHint")],
        "status": "failed" if rewrite_failure(rewrite) else rewrite["status"],
        "issues": [
            {key: issue[key] for key in ("code", "message", "retryable")}
            for issue in rewrite["issues"]
        ],
        "extensions": {
            "finance": {key: rewrite[key] for key in ("broadUniverse", "safety", "execution")}
        },
    }


def plan_projection(plan):
    composition = plan["composition"]
    return {
        "schemaVersion": 1,
        "runId": plan["runId"],
        "status": "failed"
        if plan["status"] == "failed" or rewrite_failure(plan.get("queryRewrite"))
        else plan["status"]
        if plan["status"] in {"planned", "refused"}
        else "needs_clarification",
        "profile": {
            "id": composition["profile"]["id"],
            "version": composition["profile"]["version"],
            "domainPacks": composition["domainPacks"],
            "deliveryPack": composition["deliveryPack"],
            "compositionSha256": composition["sha256"],
        },
        "capabilityId": plan.get("requestedCapabilityId") or plan["capabilityId"],
        "taskArtifact": ".data-agent/task.json",
        "domainPlanArtifact": ".data-agent/finance-run-plan.json",
        "expectedArtifacts": plan["expectedArtifacts"],
        "validationRuleIds": [
            f"finance.rule.{index + 1:03d}" for index in range(len(plan["validationRules"]))
        ],
        "createdAt": plan["createdAt"],
        "updatedAt": plan["updatedAt"],
    }


async def write_initial_plan(params, artifacts, *, rewriter=None, assert_active=None):
    instruction = strip_operational(params["instruction"]) or params["instruction"].strip()
    rewrite = params.get("queryRewrite")
    if rewrite is None:
        rewrite = await (rewriter or rewrite_query)(
            instruction,
            {
                "requestedCapabilityId": params.get("capabilityId")
                if params.get("capabilitySource") in {"manual", "benchmark"}
                else None,
                "requestedModel": params.get("llmModel"),
                "projectId": params.get("projectId"),
            },
        )
    catalog, previous_profile = FinanceCatalog(), artifacts.read(".data-agent/profile.json") or {}
    plan = build_plan(params, rewrite, profile=previous_profile, catalog=catalog)
    source = (
        params.get("capabilitySource")
        if params.get("capabilitySource") in {"manual", "default", "inferred"}
        else previous_profile.get("selectionSource") or "inferred"
    )
    profile = {
        "schemaVersion": 1,
        "profile": deepcopy(catalog.data["profile"]),
        "selectedCapabilityId": plan["capabilityId"],
        "composition": plan["composition"],
        "selectionSource": source,
        "updatedAt": plan["updatedAt"],
    }
    if assert_active:
        await assert_active()
    for relative, value in (
        (".data-agent/finance-run-plan.json", plan),
        (".data-agent/finance-query-rewrite.json", rewrite),
        (".data-agent/task.json", task_projection(rewrite)),
        (".data-agent/plan.json", plan_projection(plan)),
        (".data-agent/profile.json", profile),
    ):
        await artifacts.write(relative, value)
    failure, refused, clarification = (
        plan.get("failure"),
        plan["status"] == "refused",
        plan.get("clarification"),
    )
    summary = (
        failure["message"]
        if failure
        else "问题改写完成，安全策略拒绝执行：" + rewrite["safety"]["message"]
        if rewrite["status"] == "refused"
        else f"已将用户问题改写为结构化查询，并解析 {len(rewrite['resolvedSymbols'])} 个标的。"
        if rewrite["status"] == "ready"
        else f"问题改写完成，仍有 {len(rewrite['unresolvedTargets']) + len(rewrite['ambiguousTargets'])} 个标的需要确认。"
    )
    common = {"stage": "planning", "run_id": params["requestId"], "created_at": plan["createdAt"]}
    await artifacts.event(
        {
            **common,
            "event_type": "query_rewritten",
            "status": "error"
            if failure
            else "success"
            if rewrite["status"] in {"ready", "refused"}
            else "warning",
            "artifact_path": ".data-agent/finance-query-rewrite.json",
            "summary": summary,
        }
    )
    summary = (
        failure["message"]
        if failure
        else rewrite["safety"].get("message") or "任务已被安全策略拒绝。"
        if refused
        else "任务缺少关键输入，需要先向用户澄清：" + "；".join(clarification["questions"])
        if clarification
        else "已生成"
        + catalog.capability(plan["capabilityId"])["name"]
        + "计划，下一步将按计划解析标的、获取真实数据并生成可视化产物。"
    )
    await artifacts.event(
        {
            **common,
            "event_type": "planning_failed"
            if failure
            else "intent_refused"
            if refused
            else "intent_clarification_required"
            if clarification
            else "run_planned",
            "status": "error" if failure else "warning" if clarification or refused else "success",
            "artifact_path": ".data-agent/finance-run-plan.json",
            "summary": summary,
        }
    )
    return plan
