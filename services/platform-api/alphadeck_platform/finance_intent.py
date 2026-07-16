import json
import re

from .config import ROOT
from .messages import js_length
from .prefetch_planning import CAPABILITIES
from .query_rewrite import rewrite_failure

CATALOG = json.loads((ROOT / "config/finance-planning-catalog.json").read_text())
PATTERNS = {
    key: re.compile(value["source"], re.I if "i" in value["flags"] else 0)
    for key, value in CATALOG["intentPatterns"].items()
}


def known_symbols(text):
    haystack, candidates = text.lower(), []
    for index, alias in enumerate(CATALOG["aliases"]):
        needle, start = alias["keyword"].lower(), 0
        while needle and (start := haystack.find(needle, start)) >= 0:
            candidates.append({**alias, "start": start, "end": start + len(needle), "index": index})
            start += 1
    candidates.sort(key=lambda row: (-(row["end"] - row["start"]), row["start"], row["index"]))
    selected = []
    for candidate in candidates:
        if not any(
            candidate["start"] < value["end"] and candidate["end"] > value["start"]
            for value in selected
        ):
            selected.append(candidate)
    selected.sort(key=lambda row: (row["start"], row["index"]))
    return list(dict.fromkeys(row["symbol"] for row in selected))


def normalize(instruction):
    return re.sub(r"\s+", " ", instruction).strip()


def assess_intent(params):
    instruction = normalize(params["instruction"])
    capability = params.get("capabilityId")

    def test(name):
        return bool(PATTERNS[name + "_PATTERN"].search(instruction))

    if not instruction or not (test("FINANCIAL_KEYWORD") or capability in CAPABILITIES):
        return {
            "required": False,
            "reason": "当前请求不是需要平台量化取数的金融分析任务。",
            "missing": [],
            "questions": [],
            "confidence": 0.9,
        }
    codes = re.findall(r"\b[6035][0-9]{5}\b", instruction, re.ASCII)
    count = len(
        set(
            [
                *(symbol for symbol in params.get("symbols", []) if symbol),
                *codes,
                *known_symbols(instruction),
            ]
        )
    )
    broad = params.get("broadUniverse") is True
    image = params.get("hasImageAttachments") is True and (
        test("IMAGE_CONTEXT_TARGET") or capability == "portfolio_risk"
    )
    target = count > 0 or test("BROAD_MARKET_TARGET") or broad or image
    comparison = test("COMPARISON") or capability == "asset_comparison"
    recommendation = test("RECOMMENDATION")
    goal = test("GOAL_KEYWORD") or bool(
        params.get("semanticFocusId") and params["semanticFocusId"] != "comprehensive"
    )
    missing = []
    if not target and not comparison and (js_length(instruction) <= 18 or recommendation or goal):
        missing.append("target")
    if comparison and count < 2 and not broad:
        missing.append("comparison_universe")
    if recommendation and not test("INVESTMENT_CONSTRAINT") and not image and not broad:
        missing.append("investment_constraints")
    if target and not goal and not recommendation:
        missing.append("analysis_goal")
    questions = []
    if "target" in missing:
        questions.append("你想分析哪个股票、指数或 ETF？请给名称或代码。")
    if "comparison_universe" in missing:
        questions.append("你要对比哪些标的？请给至少两个名称或代码。")
    if "investment_constraints" in missing:
        questions.append(
            "这是投资建议类问题，请补充投资周期、风险偏好和市场范围；我会基于数据做分析，不直接给确定性买卖结论。"
            if recommendation
            else "请补充投资周期、风险偏好或约束条件，方便后续做风险口径一致的分析。"
        )
    if "analysis_goal" in missing:
        questions.append(
            "你更希望比较行情趋势、基本面、估值、风险，还是综合评分？"
            if comparison
            else "你更关注行情技术、基本面、公告事件、回测，还是综合诊断？"
        )
    result = {
        "required": bool(missing),
        "reason": "任务缺少关键输入：" + ", ".join(missing) + "。"
        if missing
        else "任务意图足够明确，可进入取数、证据和看板生成流程。",
        "missing": missing,
        "questions": questions[:3],
        "confidence": 0.82 if missing else 0.86,
    }
    if not missing:
        result["defaults"] = (
            (
                [
                    "未给具体标的时默认使用本地 A 股股票池做候选筛选。",
                    "推荐/买入类问题默认按短线候选研究口径输出，不作为确定性交易指令。",
                ]
                if broad
                else []
            )
            + (
                ["使用时间范围：" + params["timeRange"]]
                if params.get("timeRange")
                else ["未指定时间范围时默认使用最近 120 个交易日或最近报告期。"]
            )
            + ["未指定输出形式时默认生成可验证的量化看板。"]
            + (
                [
                    "图片/截图任务会先识别附件中的标的、持仓、成本、现金和盈亏字段，再进入取数分析；识别不确定的字段必须在结果中标注。"
                ]
                if image
                else []
            )
        )
    return result


def clarification_message(value):
    questions = value.get("questions") or ["请补充你想分析的标的、时间范围和关注方向。"]
    return "\n".join(
        [
            "我需要先补充几个关键信息，再开始取数和生成看板：",
            "",
            *(str(index + 1) + ". " + question for index, question in enumerate(questions)),
            "",
            "补充后我会先生成 run_plan，再获取真实数据、写入证据文件，最后生成可验证的可视化看板。",
        ]
    )


def clarification_continuation(params):
    previous = params.get("previousPlan")
    original, response = (
        normalize((previous or {}).get("question") or ""),
        normalize(params["instruction"]),
    )
    if (
        params.get("reset")
        or not previous
        or previous.get("status") != "needs_clarification"
        or rewrite_failure(previous.get("queryRewrite"))
        or not (previous.get("clarification") or {}).get("required")
        or not original
        or not response
    ):
        return None
    missing = previous["clarification"]["missing"]
    label = next(
        (
            label
            for key, label in (
                ("comparison_universe", "补充对比标的"),
                ("target", "补充标的"),
                ("investment_constraints", "补充投资约束"),
                ("analysis_goal", "补充分析方向"),
            )
            if key in missing
        ),
        "补充信息",
    )
    return {
        "previousRunId": previous["runId"],
        "originalQuestion": original,
        "userResponse": response,
        "resolvedInstruction": original + "\n" + label + "：" + response,
        "displayInstruction": "承接上一轮澄清\n原始问题："
        + original
        + "\n补充信息："
        + normalize(params.get("displayInstruction") or params["instruction"]),
        "missing": missing,
    }
