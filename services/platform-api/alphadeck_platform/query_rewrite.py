import asyncio
import json
import math
import re
import time
import unicodedata
from datetime import UTC, datetime

import httpx

from .config import ROOT, market_base_url
from .llm_config import project_llm_config
from .messages import js_length
from .research_time import resolve_research_time

CATALOG = json.loads((ROOT / "config/query-rewrite-catalog.json").read_text())
FOCUS = {
    "comprehensive": "综合诊断",
    "technical": "趋势与风险",
    "fundamental": "财务与估值",
    "events": "公告与事件",
    "comparison": "标的对比",
    "strategy": "策略研究",
    "backtest": "策略回测",
    "portfolio_risk": "持仓与组合风险",
}
CAPABILITIES = {
    "portfolio_risk": "portfolio_risk",
    "backtest": "backtest_review",
    "strategy": "strategy_research",
    "comparison": "asset_comparison",
    "fundamental": "fundamental_analysis",
    "technical": "technical_analysis",
}
UNITS = {"trading_day", "day", "week", "month", "quarter", "reporting_period", "year", "date_range"}
SYMBOL = re.compile(r"[6035][0-9]{5}\Z")


def string(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def normalize_query(value):
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def normalize_security(value):
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))
    return re.sub(r"(?:股份有限公司|有限责任公司)$", "", value).lower()


def strip_security_suffix(value):
    candidate = value.strip()
    while True:
        previous = candidate
        candidate = re.sub(
            r"(?:(?:这|那)(?:个|只|支|家)?|该(?:只|支|家)?)(?:股票|个股|证券|公司|企业|股)?$",
            "",
            candidate,
        )
        candidate = re.sub(r"(?:股票|个股)$", "", candidate).strip()
        if candidate == previous:
            return candidate


def is_han(character):
    point = ord(character)
    return any(start <= point <= end for start, end in CATALOG["hanRanges"])


def rank_symbols(query, payload):
    rows = payload.get("results") if isinstance(payload, dict) else None
    ranked = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol, name = (
            string(row.get("symbol")) or string(row.get("code")) or "",
            string(row.get("name")) or "",
        )
        if not SYMBOL.fullmatch(symbol):
            continue
        asset = (string(row.get("asset_type")) or string(row.get("assetType")) or "").lower()
        market = (string(row.get("market")) or "").upper()
        normalized_query, normalized_name = normalize_security(query), normalize_security(name)
        score = 120 if SYMBOL.fullmatch(query) and symbol == query else 0
        score += (
            100
            if normalized_name == normalized_query
            else 55
            if normalized_query in normalized_name or normalized_name in normalized_query
            else 0
        )
        score += 20 if asset == "stock" else 12 if asset in {"index", "fund", "etf"} else 0
        score += 5 if market in {"SH", "SZ", "BJ"} else 0
        resolved = {
            "query": query,
            "symbol": symbol,
            "name": name or symbol,
            "market": string(row.get("market")),
            "assetType": string(row.get("asset_type")) or string(row.get("assetType")),
            "secid": string(row.get("secid")),
            "source": string(row.get("source")),
            "confidence": min(0.99, max(0.55, score / 145)),
        }
        ranked.append((score, resolved))
    ranked.sort(key=lambda item: (-item[0], item[1]["symbol"]))
    if not ranked:
        return {"selected": None, "ambiguous": []}
    top = [resolved for score, resolved in ranked if score == ranked[0][0]]
    return (
        {"selected": None, "ambiguous": top[:5]}
        if len(top) > 1 and ranked[0][0] < 100
        else {"selected": ranked[0][1], "ambiguous": []}
    )


def literal_evidence(query, value, maximum=160):
    value = unicodedata.normalize("NFKC", value).strip() if isinstance(value, str) else ""
    return value if value and js_length(value) <= maximum and value in query else None


def merge_semantics(query, llm, maximum, reference):
    candidates = llm.get("targetCandidates")
    if not isinstance(candidates, list):
        return None
    normalized = normalize_security(query)
    explicit = re.findall(r"\b[6035][0-9]{5}\b", query, re.ASCII)
    safe = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        candidate = re.sub(r"\s+", "", unicodedata.normalize("NFKC", candidate).strip())
        if not 0 < js_length(candidate) <= 24 or not any(
            is_han(char) or (char.isascii() and char.isalnum()) for char in candidate
        ):
            continue
        supported = (
            candidate in explicit
            if SYMBOL.fullmatch(candidate)
            else normalize_security(candidate) in normalized
        )
        if supported and candidate not in safe:
            safe.append(candidate)
    safe.sort(key=query.find)
    safe = safe[:maximum]
    if candidates and not safe:
        return None
    focus, output = llm.get("analysisFocusId"), llm.get("outputIntent")
    if not isinstance(focus, str) or focus not in FOCUS or output not in ("dashboard", "answer"):
        return None
    answer_evidence = literal_evidence(query, llm.get("answerOnlyEvidence"))
    effective_output = "answer" if output == "answer" and answer_evidence else "dashboard"
    guarded = (
        ["outputIntent"]
        if effective_output != output or (output == "dashboard" and answer_evidence)
        else []
    )
    confidence = llm.get("confidence")
    if (
        type(confidence) not in {int, float}
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        return None
    raw_range, temporal = llm.get("timeRange"), None
    if raw_range:
        label = unicodedata.normalize("NFKC", raw_range.get("label", "")).strip()
        if (
            not label
            or js_length(label) > 64
            or raw_range.get("unit") not in UNITS
            or not literal_evidence(query, raw_range.get("evidence"))
        ):
            return None
        value = raw_range.get("value")
        if "value" in raw_range and (type(value) is not int or not 0 < value <= 5000):
            return None
        try:
            resolve_research_time(raw_range, reference)
        except (TypeError, ValueError):
            return None
        temporal = {
            "label": label,
            **{
                key: raw_range[key]
                for key in ("startDate", "endDate", "asOf")
                if raw_range.get(key)
            },
            **({"value": value} if "value" in raw_range else {}),
            "unit": raw_range["unit"],
            "source": "explicit",
        }
    if llm.get("broadUniverse") and not literal_evidence(query, llm.get("broadUniverseEvidence")):
        return None
    return {
        "draft": {
            "targetCandidates": safe,
            "timeRange": temporal,
            "analysisFocus": {"id": focus, "label": FOCUS[focus]},
            "outputIntent": effective_output,
            "broadUniverse": llm.get("broadUniverse", False),
        },
        "guardedFields": guarded,
    }


async def default_resolver(query, count):
    base = market_base_url()
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get(
            base + "/api/v1/symbols/resolve", params={"query": query, "count": count}
        )
        response.raise_for_status()
        return response.json()


def covered(target, resolved):
    def identity(value):
        return "".join(
            char
            for char in normalize_security(value)
            if is_han(char) or "A" <= char <= "Z" or "0" <= char <= "9"
        )

    normalized = identity(target)
    return bool(
        normalized
        and any(
            normalized in {identity(item["symbol"]), identity(item["name"])}
            or (js_length(normalized) >= 4 and normalized in identity(item["name"]))
            for item in resolved
        )
    )


async def resolve_targets(targets, resolver):
    async def resolve(target):
        try:
            ranked = rank_symbols(target, await resolver(target, 5))
            if ranked["selected"]:
                return {"resolved": ranked["selected"]}
            if ranked["ambiguous"]:
                return {
                    "ambiguous": {"query": target, "candidates": ranked["ambiguous"]},
                    "issue": {
                        "code": "TARGET_AMBIGUOUS",
                        "message": f"“{target}”存在多个同优先级证券候选，需要确认。",
                        "target": target,
                        "retryable": False,
                    },
                }
            return {
                "unresolved": target,
                "issue": {
                    "code": "TARGET_NOT_FOUND",
                    "message": f"未找到与“{target}”匹配的证券。",
                    "target": target,
                    "retryable": False,
                },
            }
        except Exception:
            return {
                "unresolved": target,
                "issue": {
                    "code": "SYMBOL_RESOLVER_UNAVAILABLE",
                    "message": "证券解析服务暂不可用，无法核实研究标的，请稍后重新发起研究。",
                    "target": target,
                    "retryable": True,
                },
            }

    outcomes = await asyncio.gather(*(resolve(target) for target in targets))
    resolved, seen = [], set()
    for result in outcomes:
        item = result.get("resolved")
        if item and item["symbol"] not in seen:
            seen.add(item["symbol"])
            resolved.append(item)
    return {
        "resolvedSymbols": resolved,
        "unresolvedTargets": [
            item["unresolved"]
            for item in outcomes
            if "unresolved" in item and not covered(item["unresolved"], resolved)
        ],
        "ambiguousTargets": [
            item["ambiguous"]
            for item in outcomes
            if "ambiguous" in item and not covered(item["ambiguous"]["query"], resolved)
        ],
        "issues": [
            item["issue"]
            for item in outcomes
            if "issue" in item and not covered(item["issue"]["target"], resolved)
        ],
    }


def default_execution(status):
    return {
        "attempted": False,
        "applied": False,
        "trigger": None,
        "status": status,
        "provider": None,
        "model": None,
        "durationMs": None,
        "semanticConfidence": None,
        "guardedFields": [],
        "errorCode": None,
        "usage": None,
    }


def rewrite_failure(rewrite):
    for issue in (rewrite or {}).get("issues", []):
        if issue["code"] in {"QUERY_REWRITE_LLM_UNAVAILABLE", "SYMBOL_RESOLVER_UNAVAILABLE"}:
            return {key: issue[key] for key in ("code", "message", "retryable")}
    if rewrite and (
        rewrite.get("status") == "failed"
        or rewrite.get("execution", {}).get("strategy") == "llm_unavailable"
    ):
        return {
            "code": "QUERY_REWRITE_LLM_UNAVAILABLE",
            "message": "研究规划未完成，请检查模型服务后重新发起研究。",
            "retryable": False,
        }
    return None


async def rewrite_query(query, options=None, *, semantic_rewriter=None, resolver=None):
    options = options or {}
    normalized = normalize_query(query)
    reference = options.get("now") or datetime.now(UTC)
    reference = (
        datetime.fromisoformat(reference.replace("Z", "+00:00"))
        if isinstance(reference, str)
        else reference
    )
    maximum = int(min(8, max(1, options.get("maxTargets", 8))))
    focus = {"id": "comprehensive", "label": FOCUS["comprehensive"]}
    safety = {"decision": "allow", "code": None, "message": None}
    base = {
        "schemaVersion": 5,
        "originalQuery": query,
        "normalizedQuery": normalized,
        "capabilityHint": options.get("requestedCapabilityId") or "stock_diagnosis",
        "targetCandidates": [],
        "resolvedSymbols": [],
        "unresolvedTargets": [],
        "ambiguousTargets": [],
        "timeRange": None,
        "analysisFocus": focus,
        "outputIntent": "dashboard",
        "broadUniverse": False,
    }
    if re.search(
        r"(?:(?:一定|保证|确保|必然|百分之百|100%)\s*(?:能|会|可以)?\s*(?:涨|赚钱|盈利|涨停|翻倍)|(?:稳赚|包赚|必涨|必赚|涨停股|明天涨停))",
        normalized,
        re.I,
    ) and re.search(
        r"(?:买|推荐|选|哪只|哪个|股票|个股|标的|明天|次日|预测|告诉我)", normalized, re.I
    ):
        safety = {
            "decision": "refuse",
            "code": "GUARANTEED_RETURN_REQUEST",
            "message": "无法承诺或预测某只证券一定上涨、涨停或盈利。可以改为基于真实数据筛选候选，并明确依据、风险和不确定性。",
        }
        return {
            **base,
            "rewrittenQuery": safety["message"],
            "status": "refused",
            "confidence": 0.99,
            "outputIntent": "answer",
            "safety": safety,
            "issues": [{"code": safety["code"], "message": safety["message"], "retryable": False}],
            "execution": {"strategy": "safety_refusal", "llm": default_execution("not_applicable")},
        }
    if semantic_rewriter is None:
        from .semantic_rewriter import configured_rewriter

        semantic_rewriter = configured_rewriter
    timeout = options.get(
        "llmTimeoutMs",
        project_llm_config(options.get("requestedModel"))["queryRewrite"]["timeoutMs"],
    )
    timeout = timeout if type(timeout) is int and 500 <= timeout <= 15000 else 4000
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout / 1000):
            outcome = await semantic_rewriter(
                {
                    "originalQuery": query,
                    "normalizedQuery": normalized,
                    "trigger": "primary",
                    "referenceTime": reference.astimezone(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    **{
                        key: options[key]
                        for key in ("requestedModel", "projectId")
                        if key in options
                    },
                }
            )
    except TimeoutError:
        outcome = {"ok": False, "code": "LLM_TIMEOUT", "retryable": True}
    except Exception:
        outcome = {"ok": False, "code": "LLM_REWRITE_FAILED", "retryable": True}
    execution = {
        **default_execution("failed"),
        "attempted": True,
        "trigger": "primary",
        "durationMs": max(0, math.floor((time.monotonic() - started) * 1000 + 0.5)),
    }
    draft, retryable = None, True
    if not outcome["ok"]:
        code = outcome["code"]
        execution.update(
            attempted=code != "LLM_NOT_CONFIGURED",
            provider=outcome.get("provider"),
            model=outcome.get("model"),
            errorCode=code,
            status={
                "LLM_TIMEOUT": "timed_out",
                "LLM_NOT_CONFIGURED": "skipped_unconfigured",
                "LLM_INVALID_OUTPUT": "invalid_output",
            }.get(code, "failed"),
        )
        retryable = outcome["retryable"]
    else:
        merged = merge_semantics(normalized, outcome["data"], maximum, reference)
        execution.update(
            provider=outcome["provider"],
            model=outcome["model"],
            semanticConfidence=outcome["data"]["confidence"],
            usage=outcome.get("usage"),
        )
        if merged:
            draft = merged["draft"]
            execution.update(
                guardedFields=merged["guardedFields"],
                applied=True,
                status="applied",
                errorCode=None,
            )
        else:
            execution.update(status="invalid_output", errorCode="LLM_INVALID_OUTPUT")
    if not draft:
        message = {
            "skipped_unconfigured": "当前模型尚未配置，无法开始研究。请配置可用模型后重新发起研究。",
            "timed_out": "模型响应超时，研究规划未完成，请稍后重新发起研究。",
            "invalid_output": "模型未能生成有效的研究计划，请重试或选择其他可用模型。",
        }.get(execution["status"], "模型服务暂时不可用，研究规划未完成，请检查服务后重新发起研究。")
        return {
            **base,
            "rewrittenQuery": message,
            "status": "failed",
            "confidence": 0,
            "safety": safety,
            "issues": [
                {
                    "code": "QUERY_REWRITE_LLM_UNAVAILABLE",
                    "message": message,
                    "retryable": retryable,
                }
            ],
            "execution": {"strategy": "llm_unavailable", "llm": execution},
        }
    targets = draft["targetCandidates"][:maximum]
    resolved = await resolve_targets(targets, resolver or default_resolver)
    symbols, unresolved, ambiguous, issues = (
        resolved[key]
        for key in ("resolvedSymbols", "unresolvedTargets", "ambiguousTargets", "issues")
    )
    status = (
        "failed"
        if any(issue["code"] == "SYMBOL_RESOLVER_UNAVAILABLE" for issue in issues)
        else "needs_clarification"
        if ambiguous or (not targets and not draft["broadUniverse"])
        else ("partial" if symbols else "needs_clarification")
        if unresolved
        else "ready"
    )
    confidence = (
        (
            min(
                *(item["confidence"] for item in symbols),
                execution["semanticConfidence"]
                if execution["semanticConfidence"] is not None
                else 1,
            )
            if symbols
            else execution["semanticConfidence"]
            if execution["semanticConfidence"] is not None
            else 0.86
        )
        if status == "ready"
        else 0.68
        if status == "partial"
        else 0.45
    )
    canonical = [
        item["name"]
        + "（"
        + item["symbol"]
        + ("." + item["market"] if item["market"] else "")
        + "）"
        for item in symbols
    ]
    target_text = (
        "、".join(canonical or targets)
        if canonical or targets
        else "用户指定的市场范围"
        if draft["broadUniverse"]
        else "待确认标的"
    )
    text = f"分析对象：{target_text}；分析重点：{draft['analysisFocus']['label']}；时间范围：{draft['timeRange']['label'] if draft['timeRange'] else '使用能力默认周期'}；输出方式：{'生成可验证看板' if draft['outputIntent'] == 'dashboard' else '只做分析问答'}"
    return {
        **base,
        **draft,
        **resolved,
        "targetCandidates": targets,
        "rewrittenQuery": text,
        "status": status,
        "confidence": confidence,
        "capabilityHint": options.get("requestedCapabilityId")
        or CAPABILITIES.get(draft["analysisFocus"]["id"], "stock_diagnosis"),
        "safety": safety,
        "execution": {"strategy": "llm_primary", "llm": execution},
    }
