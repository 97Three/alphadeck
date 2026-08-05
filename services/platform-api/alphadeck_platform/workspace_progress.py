import hashlib
import logging
import re
from datetime import UTC, datetime

from .finance_catalog import FinanceCatalog
from .messages import insert_message, js_length, js_slice, serialize_message
from .realtime import Realtime

LABELS = [
    "正在理解问题",
    "正在准备数据与证据",
    "正在分析并生成工作区",
    "正在执行平台校验",
    "已完成/未完成",
]


def strip_operational(value):
    value = value.strip()
    for marker in (
        "请默认使用中文输出可见的执行过程摘要。",
        "Visible process instructions",
        "可见过程叙述要求",
        "平台执行要求",
    ):
        if marker in value:
            value = value.split(marker, 1)[0].strip()
    return value


def bounded(value, maximum=160):
    text = re.sub(r"\s+", " ", (value or "").replace("\0", " ")).strip()
    return (
        text
        if js_length(text) <= maximum
        else js_slice(text, 0, max(1, maximum - 1)).rstrip() + "…"
    )


def summary(values, fallback="按任务合同选择", limit=4):
    values = [text for item in values or [] if (text := bounded(item, 42))]
    return (
        "、".join(values[:limit]) + (f" 等 {len(values)} 项" if len(values) > limit else "")
        if values
        else fallback
    )


def recognition(plan):
    capability = FinanceCatalog().capability(
        plan.get("requestedCapabilityId") or plan["capabilityId"]
    )
    question = strip_operational(plan["question"])
    visual = plan["visualization"]
    universe = (
        "、".join(plan["symbols"])
        if plan["symbols"]
        else "A 股股票池"
        if visual["templateId"] == "stock-selection"
        else "待从问题或附件确认"
    )
    explicit = bool(
        re.search(
            r"20\d{2}[-/.年]|(?:最近|近|过去)\s*(?:\d+|一|两|三|半)\s*(?:个)?(?:交易日|日|天|周|月|年|季度|报告期)|今日|今天|昨日|本周|本月|本季|今年|\d+\s*(?:个)?(?:交易日|日|天|周|月|年|季度|报告期)|截至",
            question,
            re.I | re.ASCII,
        )
    )
    source = (plan.get("timeRange") or "") + " " + " ".join(plan["dataRequirements"])
    granularity = (
        "分钟/分时"
        if re.search("分钟|分时|小时", source)
        else "报告期"
        if re.search("报告期|季度|财务|基本面", source)
        or capability["id"] == "fundamental_analysis"
        else "日"
        if re.search(r"交易日|日线|K\s*线|行情|走势|回测|年|月|周", source, re.I)
        else "不适用"
    )
    view = (
        visual.get("variantName")
        if visual.get("variantName") is not None
        else visual.get("name")
        if visual.get("name") is not None
        else capability["name"]
    )
    rows = [
        ["业务场景", capability["name"], "明确"],
        [
            "分析对象",
            universe,
            "明确" if plan["symbols"] or visual["templateId"] == "stock-selection" else "待确认",
        ],
        [
            "时间范围",
            plan.get("timeRange") if plan.get("timeRange") is not None else "未指定",
            "不适用" if not plan.get("timeRange") else "明确" if explicit else "平台默认",
        ],
        ["时间粒度", granularity, "不适用" if granularity == "不适用" else "明确"],
        ["核心数据/指标", summary(plan["dataRequirements"], "按任务合同核验"), "明确"],
        ["分析维度", view, "明确" if view else "不适用"],
        ["输出形式", "交互式金融看板" if visual["required"] else "结构化分析", "明确"],
    ]

    def cell(value):
        return bounded(value).replace("\\", "\\\\").replace("|", "\\|")

    return "\n".join(
        [
            "| 维度 | 初步识别 | 状态 |",
            "| --- | --- | --- |",
            *("| " + " | ".join(cell(item) for item in row) + " |" for row in rows),
        ]
    )


def build_message(options):
    stage, plan = options["stage"], options.get("runPlan")
    title = f"**【进度 {stage}/5】{LABELS[stage - 1]}**"
    if stage == 1:
        if not plan:
            return title + "\n\n正在建立任务合同并识别分析对象、时间范围、数据需求和输出形式。"
        return "\n".join(
            [
                title,
                "",
                recognition(plan),
                "",
                "用户原问句："
                + (bounded(strip_operational(plan["question"]), 240) or "未提供文字问题"),
                "",
                "初步识别发现关键输入仍不唯一，先完成必要澄清，再进入数据核验。"
                if plan["status"] == "needs_clarification"
                else "已完成初步语义识别，开始核验真实数据和任务合同。",
            ]
        )
    if stage == 2:
        symbols = "、".join(plan["symbols"]) if plan and plan["symbols"] else "任务对象"
        time = (
            plan.get("timeRange")
            if plan and plan.get("timeRange") is not None
            else "任务要求的时间范围"
        )
        return (
            title
            + f"\n\n正在按 {bounded(symbols, 80)}、{bounded(time, 80)} 核验本地数据覆盖、真实信源和字段完整性。\n\n当前 Skills：{summary(options.get('skillIds'), limit=6)}。"
        )
    if stage == 3:
        return (
            title
            + "\n\n开始基于任务合同、已获取数据与可用 Skills 生成工作区；缺失字段会继续补齐或明确标注。"
            + (
                "\n\n当前 Skills：" + summary(options["skillIds"], limit=6) + "。"
                if options.get("skillIds")
                else ""
            )
        )
    if stage == 4:
        return (
            title
            + "\n\n正在检查构建、数据绑定、证据文件、图表、响应式布局和持久预览；失败项会进入受限修复。"
        )
    if options.get("cancelledReason"):
        return (
            "**【进度 5/5】已暂停**\n\n请求已暂停，当前候选不会被投影为完成态。\n\n原因："
            + bounded(options["cancelledReason"], 320)
        )
    if options.get("failureReason"):
        return (
            "**【进度 5/5】未完成**\n\n当前候选未通过生产完成条件，未投影为可交付结果。\n\n原因："
            + bounded(options["failureReason"], 320)
        )
    count, warnings = options.get("validationCheckCount"), options.get("validationWarningCount")
    check = (
        f"{count} 项检查完成" + (f"（{warnings} 项提示）" if warnings else "，无阻断项")
        if count is not None
        else "通过"
    )
    return (
        "**【进度 5/5】已完成**\n\n工作区已生成，并通过平台自动校验与独立证据验收。\n\n- 自动校验："
        + check
        + "\n- 独立证据验收：通过\n- 持久预览："
        + (bounded(options["previewUrl"], 240) if options.get("previewUrl") else "已就绪")
    )


def parse_context(value):
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != 1
        or value.get("source") != "estimated"
        or type(value.get("compacted")) is not bool
    ):
        return None
    for key in ("runId", "model"):
        if (
            not isinstance(value.get(key), str)
            or not value[key].strip()
            or js_length(value[key]) > 256
            or re.search(r"[\x00-\x1f\x7f]", value[key])
        ):
            return None
    keys = (
        "turn",
        "observedAt",
        "inputTokens",
        "inputBudgetTokens",
        "contextWindowTokens",
        "reservedOutputTokens",
    )
    if any(
        type(value.get(key)) is not int or not 0 <= value[key] <= 9007199254740991 for key in keys
    ):
        return None
    if (
        value["turn"] < 1
        or value["observedAt"] > 8640000000000000
        or value["inputBudgetTokens"] < 1
        or value["contextWindowTokens"] < 1
        or value["inputTokens"] > value["inputBudgetTokens"]
        or value["inputBudgetTokens"] > value["contextWindowTokens"] - value["reservedOutputTokens"]
    ):
        return None
    return {
        key: value[key] for key in ("schemaVersion", "runId", "model", *keys, "source", "compacted")
    }


async def turn_metrics(database, project, request):
    root = await database.fetch_one(
        "SELECT created_at,completed_at FROM user_requests WHERE project_id=%s AND id=%s",
        (project, request),
    )
    if not root:
        raise ValueError("Turn metrics root request is missing")
    # Only platform-created repair requests bound to this project belong to the lineage.
    rows = await database.fetch_all(
        """SELECT r.*, e.event_type AS usage_event_type,e.payload AS usage_payload
        FROM agent_runs r LEFT JOIN LATERAL (SELECT event_type,payload FROM agent_events WHERE run_id=r.id AND event_type IN ('run_finished','usage') ORDER BY sequence DESC LIMIT 1) e ON true
        WHERE r.project_id=%s AND (r.request_id=%s OR r.request_id=%s OR r.request_id LIKE %s)
        ORDER BY r.created_at DESC,r.id DESC""",
        (
            project,
            request,
            request + "-validation-repair",
            request.replace("_", r"\_") + "-validation-repair-%",
        ),
    )
    if len({row["request_id"] for row in rows} | {request}) > 64:
        raise ValueError("Turn metrics lineage exceeds the limit")

    def source(row):
        payload = row["usage_payload"] or {}
        usage = payload.get("totalUsage" if row["usage_event_type"] == "usage" else "usage") or {}
        return usage.get("usageSource") if isinstance(usage, dict) else None

    sources = [source(row) for row in rows if row["total_tokens"] > 0]
    accounting = (
        "partial"
        if any(
            row["status"] in {"pending", "running", "reconciling", "waiting", "interrupted"}
            or source(row) not in {"provider", "estimated", "cache_estimated", "mixed"}
            for row in rows
        )
        else "mixed"
        if "mixed" in sources
        or "estimated" in sources
        and any(item != "estimated" for item in sources)
        else "estimated"
        if sources and all(item == "estimated" for item in sources)
        else "provider"
    )
    result = {
        "schemaVersion": 1,
        "elapsedMs": max(
            0,
            int(
                ((root["completed_at"] or datetime.now(UTC)) - root["created_at"]).total_seconds()
                * 1000
            ),
        ),
        "agentRunCount": len(rows),
        "tokenAccounting": accounting,
    }
    for public, stored in (
        ("modelTurnCount", "turn_count"),
        ("inputTokens", "input_tokens"),
        ("outputTokens", "output_tokens"),
        ("totalTokens", "total_tokens"),
        ("cachedInputTokens", "cached_input_tokens"),
        ("cacheMissInputTokens", "cache_miss_input_tokens"),
        ("reasoningTokens", "reasoning_tokens"),
    ):
        total = sum(row[stored] for row in rows)
        if not 0 <= total <= 9007199254740991:
            raise ValueError("Turn metrics exceed safe integer range")
        result[public] = total
    snapshot = (
        parse_context((rows[0]["usage_payload"] or {}).get("contextSnapshot")) if rows else None
    )
    if snapshot and snapshot["runId"] == rows[0]["id"]:
        result["contextSnapshot"] = snapshot
    return result


class WorkspaceProgress:
    def __init__(self, database, project, request, conversation=None):
        self.database, self.project, self.request, self.conversation = (
            database,
            project,
            request,
            conversation,
        )
        self.published = set()

    async def __call__(self, options):
        stage = options["stage"]
        if stage in self.published:
            return
        try:
            successful = (
                stage == 5
                and not options.get("failureReason")
                and not options.get("cancelledReason")
            )
            metadata = {
                "isWorkspaceProgress": True,
                "progressStep": stage,
                "progressTotal": 5,
                "progressStatus": "completed"
                if successful
                else "cancelled"
                if stage == 5 and options.get("cancelledReason")
                else "failed"
                if stage == 5
                else "running",
                "isMissionFinal" if stage == 5 else "isMissionIntermediate": True,
                **({"isPiAgentFinal": True, "validationPassed": True} if successful else {}),
            }
            if stage == 5:
                try:
                    metadata["turnMetrics"] = await turn_metrics(
                        self.database, self.project, self.request
                    )
                except Exception:
                    logging.warning("Turn metrics projection unavailable for %s", self.request)
            digest = hashlib.sha256(
                (self.project + "\0" + self.request + "\0" + str(stage)).encode()
            ).hexdigest()[:32]
            async with self.database.transaction() as tx:
                message = await insert_message(
                    tx,
                    self.project,
                    "assistant",
                    build_message(options),
                    identity="workspace-progress-" + digest,
                    ensure=True,
                    request_id=self.request,
                    conversation=self.conversation,
                    metadata=metadata,
                )
            self.published.add(stage)
            await Realtime(self.database).publish(
                self.project, {"type": "message", "data": serialize_message(message)}
            )
        except Exception:
            logging.warning(
                "Workspace progress projection failed at stage %s for %s", stage, self.request
            )
