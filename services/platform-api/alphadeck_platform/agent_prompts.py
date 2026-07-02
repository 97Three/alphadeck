"""Native prompt assembly; static wording is versioned separately from execution logic."""

import json
import re
from functools import lru_cache

from .config import ROOT
from .finance_catalog import FinanceCatalog
from .prepared_artifacts import assess_prepared
from .validation_inputs import read_plan
from .visualization import serialize_template


@lru_cache(maxsize=1)
def templates():
    value = json.loads((ROOT / "config/python-agent-prompt-templates.json").read_text())
    if value.get("schemaVersion") != 1:
        raise ValueError("Unsupported Agent prompt template catalogue")
    return value


def render(template, **values):
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values.get(match[1], match[0]), template)


PYTHON_API_BOUNDARY = (
    "\n\n运行环境约束：所有后端接口由 Python 执行。/api/market/** 已由平台的 "
    "vite.config.ts 转发到只读 Python 行情桥；看板采用 Vue 3 + Vite + TypeScript，"
    "页面写在 src/App.vue、样式写在 src/style.css。不要创建 TypeScript 后端接口，"
    "也不要修改平台管理的 vite.config.ts。"
)


def capability_context(plan=None):
    plan = plan or {}
    capability = FinanceCatalog().capability(
        plan.get("requestedCapabilityId") or plan.get("capabilityId")
    )
    chosen = plan.get("visualization") or {}
    serialized = serialize_template(
        capability["id"],
        {
            "instruction": plan.get("question"),
            "symbolCount": len(plan["symbols"]) if plan.get("symbols") is not None else None,
            "requestedVariantId": chosen.get("variantId"),
            "dataSignals": chosen.get("dataSignals"),
        },
    )
    visualization = {
        key: chosen.get(key) if chosen.get(key) is not None else serialized[source]
        for key, source in (
            ("templateId", "templateId"),
            ("variantId", "variantId"),
            ("variantName", "variantName"),
            ("layout", "layout"),
            ("density", "density"),
            ("firstViewport", "firstViewport"),
            ("variantGuidance", "variantGuidance"),
        )
    }
    components = chosen.get("panels") or serialized["requiredComponents"]
    llm = plan.get("llm") or {}
    rules = plan.get("validationRules") or capability["validationRules"]
    enabled = (llm.get("queryRewrite") or {}).get("enabled", True)
    return (
        "任务合同：\n"
        f"- 能力：{capability['id']} / {capability['name']}；执行能力：{plan.get('executionCapabilityId') or capability['executionCapabilityId']}\n"
        f"- LLM：{llm.get('provider') or 'openai'} / {llm.get('model') or 'local_qwen:qwen3.5-9b-q5km'}；Query Rewrite：{'LLM-first' if enabled else 'disabled（失败关闭）'}\n"
        f"- 标的：{', '.join(plan.get('symbols') or []) or '以只读运行计划为准'}\n"
        f"- 页面模板：{visualization['templateId']} / {visualization['variantId']}（{visualization['variantName']}）\n"
        f"- 布局与密度：{visualization['layout']} / {visualization['density']}\n"
        f"- 首屏：{'；'.join(visualization['firstViewport'])}\n"
        f"- 必备内容：{'；'.join(components)}\n"
        f"- 变体指导：{'；'.join(visualization['variantGuidance'])}\n"
        f"- 验收：{'；'.join(rules)}"
    )


def task_prompt(
    instruction,
    root,
    *,
    run_plan=None,
    platform_prepared=None,
    prepared_intent=None,
    phase=None,
    has_attachments=False,
):
    plan = run_plan if run_plan is not None else read_plan(root)
    prepared = (
        platform_prepared if platform_prepared is not None else assess_prepared(root, plan)["ready"]
    )
    phase = phase or ("workspace-generation" if prepared else "data-preparation")
    if phase == "validation-repair":
        plan = plan or {}
        capability = FinanceCatalog().capability(
            plan.get("requestedCapabilityId") or plan.get("capabilityId")
        )
        chosen = plan.get("visualization") or {}
        serialized = serialize_template(
            capability["id"],
            {
                "instruction": plan.get("question"),
                "symbolCount": len(plan.get("symbols", [])),
                "requestedVariantId": chosen.get("variantId"),
                "dataSignals": chosen.get("dataSignals"),
            },
        )
        return (
            "# AlphaDeck Task Packet\n\n数据阶段：validation-repair\n"
            + f"权威定位：{capability['id']}；标的 {', '.join(plan.get('symbols') or []) or '无显式标的'}；模板 {chosen.get('templateId') or serialized['templateId']} / {chosen.get('variantId') or serialized['variantId']}\n\n{instruction.strip()}"
        )
    mode = (
        0
        if not prepared
        else 4
        if has_attachments
        else 2
        if prepared_intent == "standard"
        else 3
        if prepared_intent == "custom"
        else 1
    )
    return render(
        templates()["task"][str(mode)],
        INSTRUCTION=instruction.strip(),
        CAPABILITY_CONTEXT=capability_context(plan),
    )


def system_prompt(*, phase="workspace-generation", prepared_intent=None, skill_manifest=None):
    key = phase + ":" + (prepared_intent or "none")
    # Unknown phases receive the original restrictive default phase contract.
    if key not in templates()["system"]:
        key = "planning:" + (prepared_intent or "none")
    manifest = (
        skill_manifest or ""
    ).strip() or "# PI Agent Skill Manifest\nNo task skill capsule was loaded."
    return render(templates()["system"][key], SKILL_MANIFEST=manifest) + PYTHON_API_BOUNDARY


def user_prompt(
    *,
    task_packet,
    skill_context,
    initial_dashboard_contract=None,
    require_dashboard_contract=True,
    personalization_context=None,
    governed_knowledge_context=None,
):
    catalog = templates()["user"]
    contract = (
        render(catalog["contract"], CONTRACT=initial_dashboard_contract.strip())
        if initial_dashboard_contract and initial_dashboard_contract.strip()
        else catalog["noContract" if require_dashboard_contract else "notRequired"]
    )
    personalization = (
        render(catalog["personalization"], PERSONALIZATION=personalization_context.strip())
        if personalization_context and personalization_context.strip()
        else ""
    )
    knowledge = (
        render(catalog["knowledge"], KNOWLEDGE=governed_knowledge_context.strip())
        if governed_knowledge_context and governed_knowledge_context.strip()
        else ""
    )
    return "\n\n".join(
        value
        for value in (
            task_packet.strip(),
            skill_context.strip(),
            knowledge,
            personalization,
            contract,
        )
        if value
    )
