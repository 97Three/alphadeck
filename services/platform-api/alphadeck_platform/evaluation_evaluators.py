import json
import math

from .config import ROOT
from .evaluation_queue import strings
from .evaluation_scoring import (
    DIMENSIONS,
    deterministic_gate,
    number,
    record,
    result_score,
    score,
    weighted_score,
)
from .finance_values import js_string

LABELS = dict(
    zip(
        DIMENSIONS,
        (
            "产物契约",
            "事实与证据",
            "任务完成度",
            "视觉交付",
            "运行可靠性",
            "执行效率",
            "安全与边界",
        ),
        strict=True,
    )
)


def definitions():
    return json.loads((ROOT / "config/evaluation-catalog.json").read_text())["evaluators"]


def definition(identity):
    result = next((item for item in definitions() if item["id"] == identity), None)
    if result is None:
        raise ValueError("未知评测器：" + js_string(identity))
    return result


def status_score(statuses):
    return (
        0
        if "failed" in statuses
        else 70
        if "warning" in statuses or "unknown" in statuses
        else 100
        if statuses
        else 85
    )


def score_status(value):
    return "passed" if value >= 85 else "warning" if value >= 65 else "failed"


def check_statuses(result, ids):
    checks = record(result.get("validation")).get("checks")
    return (
        [
            js_string(item.get("status") if item.get("status") is not None else "unknown")
            for raw in checks
            if (item := record(raw)).get("id") in ids
        ]
        if isinstance(checks, list)
        else []
    )


def apply_evaluator(identity, mode, value, semantic_review=None):
    chosen = definition(identity)
    if mode not in chosen["supportedModes"]:
        raise ValueError(f"{identity} 不支持 {mode} 模式")
    result = record(value)
    failures, artifacts, audit = (
        strings(result.get("failures")),
        record(result.get("artifacts")),
        record(result.get("eventAudit")),
    )
    oracle, visual, execution = (
        record(artifacts.get("oracle")),
        record(result.get("visualCheck")),
        record(result.get("agentExecution")),
    )
    usage, tools = record(execution.get("usage")), record(execution.get("tools"))
    repairs, gate = number(result.get("repairAttempts")), deterministic_gate(result)
    contract_score = status_score(
        check_statuses(
            result,
            [
                "artifact_policy",
                "vue_build",
                "preview_http_200",
                "artifact_contracts",
                "dashboard_data_binding",
                "chart_presence",
            ],
        )
    )
    grounding_statuses = check_statuses(
        result, ["final_data_file", "evidence_files", "market_proxy"]
    )
    if oracle.get("passed") is False:
        grounding_statuses.append("failed")
    if oracle.get("warning") is True:
        grounding_statuses.append("warning")
    grounding_score = status_score(grounding_statuses)
    task_score = 100 if gate["passed"] else max(0, 70 - len(failures) * 15)
    visual_statuses = check_statuses(result, ["visual_presentation"])
    if result.get("visualCheck") is not None:
        visual_statuses.append("passed" if visual.get("passed") is True else "failed")
    visual_score = status_score(visual_statuses)
    reliability = max(
        0,
        100
        - repairs * 18
        - number(audit.get("errorCount")) * 35
        - number(audit.get("warningCount")) * 4,
    )
    turns, miss = number(execution.get("turns")), number(usage.get("cacheMissInputTokens"))
    efficiency = (
        max(
            0,
            100
            - max(0, turns - 6) * 6
            - math.floor(miss / 28000) * 8
            - number(tools.get("unexpectedFailureCount")) * 30,
        )
        if mode == "e2e"
        else 100
    )
    safety_score = status_score(check_statuses(result, ["artifact_policy"]))
    checks_raw = oracle.get("checks") if isinstance(oracle.get("checks"), list) else []
    safety_checks = [
        item
        for raw in checks_raw
        if (item := record(raw)).get("target") == "page"
        and item.get("operator") in ("not_matches", "not_contains")
    ]
    if any(
        item.get("passed") is not True and item.get("severity") != "warning"
        for item in safety_checks
    ):
        safety_score = 0
    elif any(item.get("passed") is not True for item in safety_checks):
        safety_score = min(safety_score, 70)
    checks = []
    if identity == "agent-review":
        if semantic_review is None:
            checks.append(
                {
                    "id": "semantic_review",
                    "name": "语义审阅",
                    "status": "failed",
                    "summary": "Agent 评测器没有生成可验真的语义审阅结果。",
                }
            )
            task_score = grounding_score = 0
        else:
            verified = record(semantic_review.get("evidenceValidation")).get("status") == "verified"
            independent = bool(semantic_review["reviewer"]["independentFromGenerator"])
            checks.extend(
                [
                    {
                        "id": "semantic_review",
                        "name": "语义审阅",
                        "status": semantic_review["verdict"] if verified else "failed",
                        "summary": semantic_review["summary"]
                        if verified
                        else "语义审阅缺少可验证的证据引用。",
                    },
                    {
                        "id": "reviewer_independence",
                        "name": "Reviewer 独立性",
                        "status": "passed" if independent else "warning",
                        "summary": "语义 reviewer 与生成模型来源独立。"
                        if independent
                        else "语义 reviewer 与生成模型不独立，只能作为软证据。",
                    },
                ]
            )
            by_id = {item["id"]: item["score"] for item in semantic_review["dimensions"]}
            task_score = math.floor(
                (
                    by_id.get("intentCoverage", 0)
                    + by_id.get("businessCompleteness", 0)
                    + by_id.get("actionability", 0)
                )
                / 3
                + 0.5
            )
            grounding_score = min(grounding_score, by_id.get("grounding", 0))
            safety_score = min(safety_score, by_id.get("riskCommunication", 0))
    elif identity == "visual-contract":
        checks.append(
            {
                "id": "visual_contract",
                "name": "视觉契约",
                "status": score_status(visual_score),
                "summary": "多视口视觉与可访问性契约通过。"
                if visual_score >= 85
                else "视觉契约存在布局、资源或可访问性问题。",
            }
        )
    else:
        checks.append(
            {
                "id": "strict_contract",
                "name": "强规则复核",
                "status": "passed" if gate["passed"] else "failed",
                "summary": "确定性硬门全部通过。"
                if gate["passed"]
                else "确定性硬门失败：" + "、".join(gate["failures"]) + "。",
            }
        )
    dimension_scores = [
        contract_score,
        grounding_score,
        task_score,
        visual_score,
        reliability,
        efficiency,
        safety_score,
    ]
    summaries = [
        "构建、Schema、绑定和产物策略。",
        "数据文件、来源、质量与事实 oracle。",
        "用户意图、业务内容和交付完整性。",
        "多视口布局、图表、资源和可访问性。",
        f"修复 {js_string(repairs)} 次，事件错误 {js_string(number(audit.get('errorCount')))} 个。",
        f"turns={js_string(turns)}，cache-miss={js_string(miss)}。"
        if mode == "e2e"
        else "确定性链路不计算模型成本。",
        "产物执行策略、禁止性断言与风险边界。",
    ]
    dimensions = [
        {
            "id": key,
            "label": LABELS[key],
            "score": score(value),
            "weight": chosen["dimensionWeights"][key],
            "status": score_status(score(value)),
            "summary": summary,
        }
        for key, value, summary in zip(DIMENSIONS, dimension_scores, summaries, strict=True)
    ]
    return {
        "evaluatorId": identity,
        "evaluatorVersion": chosen["version"],
        "rubricVersion": chosen["rubricVersion"],
        "hardGatePassed": gate["passed"],
        "passed": gate["passed"] and all(item["status"] != "failed" for item in checks),
        "score": weighted_score(dimensions),
        "checks": checks,
        "dimensions": dimensions,
        "semanticReview": semantic_review,
    }


def is_current(value):
    value = record(value)
    chosen = next(
        (
            item
            for item in definitions()
            if item["id"] == value.get("evaluatorId")
            and item["version"] == value.get("evaluatorVersion")
        ),
        None,
    )
    if chosen is None or chosen["rubricVersion"] != value.get("rubricVersion"):
        return False
    dimensions = value.get("dimensions")
    if not isinstance(dimensions, list) or len(dimensions) != len(DIMENSIONS):
        return False
    dimensions = [record(item) for item in dimensions]
    ids = [item.get("id") for item in dimensions]
    if any(not isinstance(identity, str) for identity in ids) or len(set(ids)) != len(DIMENSIONS):
        return False
    for key in DIMENSIONS:
        dimension = next((item for item in dimensions if item.get("id") == key), {})
        value_score = number(dimension.get("score"), None)
        if (
            value_score is None
            or int(value_score) != value_score
            or not 0 <= value_score <= 100
            or type(dimension.get("weight")) not in {int, float}
            or dimension["weight"] != chosen["dimensionWeights"][key]
            or dimension.get("status") != score_status(value_score)
        ):
            return False
    checks = value.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or any(record(item).get("status") not in ("passed", "warning", "failed") for item in checks)
    ):
        return False
    expected = value.get("hardGatePassed") is True and all(
        item["status"] != "failed" for item in checks
    )
    return (
        result_score({"evaluation": value}) == value.get("score")
        and weighted_score(dimensions) == value.get("score")
        and value.get("passed") is expected
        and type(value.get("hardGatePassed")) is bool
        and type(value.get("passed")) is bool
    )
