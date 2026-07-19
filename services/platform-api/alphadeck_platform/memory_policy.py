import json
import re

from .messages import js_length
from .provenance import js_json, object_keys

STABLE = re.compile(
    r"(?:以后|今后|后续|从现在起|每次|默认|始终|一直|总是|请记住|帮我记住|我的偏好是|我(?:更)?偏好|我(?:更)?喜欢|回答时|输出时|分析时|研究时)"
)
EPHEMERAL = re.compile(r"^(?:这次|本次|这一轮|当前任务|今天|现在)(?!以后|起)")
CONTROL = re.compile(
    r"(?:授权|权限|角色|管理员|密码|口令|token|令牌|密钥|secret|绕过|忽略风控|自动交易|自动下单|自动买|自动卖|代我交易|买入|卖出|仓位|止损|止盈)",
    re.I,
)
CLASSIFIERS = [
    (
        "analysis.risk_style",
        "识别到稳定的风险表达偏好",
        r"(?:风险|不确定性|回撤|风险提示|风险因素|谨慎|保守)",
    ),
    (
        "research.evidence_style",
        "识别到稳定的证据与数据时点偏好",
        r"(?:证据|出处|来源|引用|数据时点|数据日期|可验证|可信度)",
    ),
    (
        "output.detail_level",
        "识别到稳定的回答详略偏好",
        r"(?:简洁|简短|精简|详细|详尽|展开说明|篇幅|字数)",
    ),
    (
        "output.visual_style",
        "识别到稳定的图表与呈现偏好",
        r"(?:图表|可视化|表格|K线|图形|仪表盘|看板呈现)",
    ),
    (
        "research.default_horizon",
        "识别到稳定的研究周期偏好",
        r"(?:(?:研究|分析|投资|持有).{0,8}(?:周期|期限)|短线|中线|长线|日内|周线|月线)",
    ),
    (
        "analysis.default_market",
        "识别到稳定的默认市场偏好",
        r"(?:A股|港股|美股|沪深|纳斯达克|中国市场|香港市场|美国市场)",
    ),
    (
        "output.answer_style",
        "识别到稳定的回答结构偏好",
        r"(?:先.{0,12}(?:结论|摘要)|回答.{0,12}(?:结构|格式)|分点|条列|结论先行|先说结论|先.+再.+)",
    ),
]


def detect_candidate(instruction):
    value = re.sub(r"\s+", " ", instruction).strip()
    if (
        not 4 <= js_length(value) <= 1024
        or not STABLE.search(value)
        or EPHEMERAL.search(value)
        or CONTROL.search(value)
    ):
        return None
    for key, reason, pattern in CLASSIFIERS:
        if re.search(
            pattern, value, re.I if key in {"output.visual_style", "analysis.default_market"} else 0
        ):
            return {
                "contract": "alphadeck-personal-memory-candidate/v1",
                "key": key,
                "value": value,
                "scope": "project",
                "reason": reason,
            }
    return None


def personalization_key(value):
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,255}", value) or not value.startswith(
        ("analysis.", "output.", "research.")
    ):
        raise ValueError("Unsupported personalization key.")
    return value


def preference_context(project, scope, context=None):
    result = {"product": "alphadeck"}
    if scope == "project":
        result["project_id"] = project
    for key in object_keys(context or {}):
        value = context[key]
        if (
            key not in {"product", "project_id"}
            and re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", key)
            and isinstance(value, str)
            and 0 < js_length(value) <= 512
            and len(result) < 16
        ):
            result[key] = value
    return result


def is_preference(value):
    try:
        personalization_key(value["key"])
        return value["context"].get("product") == "alphadeck"
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def select_projection(projection, project):
    memories, revisions = [], []
    for segment in projection["segments"]:
        try:
            candidate = json.loads(segment["content"])
        except ValueError:
            continue
        if not isinstance(candidate, dict):
            continue
        context, key, value = (
            candidate.get("context"),
            candidate.get("key", ""),
            candidate.get("value", ""),
        )
        if (
            not isinstance(context, dict)
            or not isinstance(key, str)
            or not isinstance(value, str)
            or not value
            or js_length(value) > 4096
        ):
            continue
        if "project_id" in context and (
            not isinstance(context["project_id"], str) or context["project_id"] != project
        ):
            continue
        string_context = {
            key: context[key] for key in object_keys(context) if isinstance(context[key], str)
        }
        if not is_preference({"key": key, "context": string_context}):
            continue
        memories.append(
            {"context": string_context, "key": personalization_key(key), "value": value}
        )
        revisions.extend(source["revisionId"] for source in segment["sources"])
    return {
        "content": js_json({"memories": memories}),
        "revisionIds": list(dict.fromkeys(revisions)),
    }
