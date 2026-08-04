import json
import re

from .config import ROOT
from .messages import js_length, js_slice
from .prefetch_market import timestamp

CATALOG = json.loads((ROOT / "config/validation-repair-catalog.json").read_text())
REPORT = ".data-agent/validation-repair-plan.json"
ORDER = [
    "src/App.vue",
    "src/style.css",
    "vite.config.ts",
    "data_file/final/**",
    "evidence/**",
]


def failed_checks(report):
    return [check for check in report["checks"] if check["status"] == "failed"]


def writable_globs(report):
    paths = list(
        dict.fromkeys(
            path
            for check in failed_checks(report)
            for path in CATALOG["scopes"].get(check["id"], [])
        )
    )
    return sorted(paths, key=lambda value: ORDER.index(value) if value in ORDER else len(ORDER))


def truncate(value, limit=1500):
    value = value.strip()
    return value if js_length(value) <= limit else js_slice(value, 0, limit) + "\n...内容已截断..."


def actions(check):
    if check["id"] == "dashboard_data_binding" and re.search(
        "交易执行计划|交易计划|买入区间|止损|目标价|仓位|操作建议",
        check["summary"] + "\n" + (check.get("details") or ""),
    ):
        return CATALOG["tradingActions"]
    return CATALOG["actions"].get(check["id"], CATALOG["actions"]["unknown"])


def repair_plan(report):
    return {
        "schemaVersion": 1,
        "status": "needed",
        "projectId": report["projectId"],
        "reportPath": report["reportPath"],
        "repairPlanPath": REPORT,
        "steps": [
            {
                "checkId": check["id"],
                "checkName": check["name"],
                "summary": check["summary"],
                "actions": actions(check),
                **({"details": truncate(check["details"], 1000)} if check.get("details") else {}),
            }
            for check in failed_checks(report)
        ],
        "createdAt": timestamp(),
    }


def chinese_list(values):
    return (
        values[0]
        if len(values) == 1
        else "无"
        if not values
        else "、".join(values[:-1]) + " 和 " + values[-1]
    )


def repair_instruction(report, options=None):
    options = options or {}
    checks, plan = failed_checks(report), repair_plan(report)
    summary = "\n".join(
        f"{index + 1}. {check['name']}（{check['id']}）：{check['summary']}"
        + ("\n   细节：" + truncate(check["details"]) if check.get("details") else "")
        for index, check in enumerate(checks)
    )
    steps = "\n".join(
        f"{index + 1}. {step['checkName']}（{step['checkId']}）\n"
        + "\n".join(
            f"   {action_index + 1}. {action}"
            for action_index, action in enumerate(step["actions"])
        )
        for index, step in enumerate(plan["steps"])
    )
    original = (
        "\n原始用户需求：\n" + truncate(options["originalInstruction"], 1000) + "\n"
        if options.get("originalInstruction")
        else ""
    )
    identifiers = "、".join(check["id"] for check in checks) or "报告状态异常但未提供失败 ID"
    paths = chinese_list(writable_globs(report))
    reads = list(
        dict.fromkeys(
            [
                ".data-agent/validation.json（仅失败项）",
                ".data-agent/validation-repair-plan.json（仅本轮步骤）",
                *(
                    value
                    for check in checks
                    for value in CATALOG["reads"].get(check["id"], CATALOG["reads"]["unknown"])
                ),
            ]
        )
    )
    completion = "\n".join(
        "- "
        + check["id"]
        + "："
        + CATALOG["completion"]
        .get(check["id"], CATALOG["completion"]["unknown"])
        .replace("CHECK_NAME", check["name"])
        for check in checks
    )
    return f"""AlphaDeck failure-scoped repair packet

目标：只修复本轮失败项，保留已有真实数据、有效分析和无关页面内容。{original}

修复范围：
- 失败 ID：{identifiers}
- 唯一可写范围：{paths}
- 整个 `.data-agent/**` 是平台只读计划、报告与状态；你不得修改它。其结构修复和重新生成由平台负责。
- 定向读取：{"；".join(reads)}
- 不要扫描或通读未被失败项指向的目录和文件。

失败项：
{summary or "无失败项，但验证报告状态为失败，请重新检查产物。"}

最小修复动作：
{steps or "请重新检查验证报告并补齐缺失产物。"}

完成条件（平台复验前候选）：
{completion or "- 报告未提供失败 ID；仅提交已能由失败详情证明的修复。"}

执行契约：
1. 使用本轮提供的 typed tools 定向读取和修改；只在需要新建失败产物时使用 write_file，否则优先 edit_file。
2. 必须实际修改失败项关联文件，但不得顺带重写未失败模块；不得写入 mock、占位数据、凭据或密钥。
3. 不要执行 shell、安装依赖、启动开发服务器、构建、预览或循环复验。构建、预览与自动验证由 AlphaDeck 平台统一执行。
4. 完成上述失败项对应修改后，调用 submit_result，artifacts 只列出本轮实际修改的工作区相对路径；提交即结束本次物理运行，等待平台独立验证。"""


async def write_repair_plan(artifacts, report):
    if report["passed"]:
        await artifacts.remove_repair_plan()
    else:
        await artifacts.write(REPORT, repair_plan(report))
