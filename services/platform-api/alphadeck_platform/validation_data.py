import re

from .baseline_evidence import FINAL, QUALITY, SOURCES, ensure_baseline, pick_string
from .data_quality import assess_dashboard
from .evidence_quality import assess_evidence
from .finance_values import coalesce, numeric, record
from .prefetch_planning import trading_plan_intent
from .provenance import js_json
from .scaffold import controlled_path
from .skill_integrity import read_regular
from .validation_inputs import (
    any_key_deep,
    comparison_symbols,
    empty_screener,
    expected_template,
    fetched_symbols,
    inspect_payload,
    planned_symbols,
    read_json,
    read_plan,
    read_text,
    text_for_intent,
)

SENSITIVE = re.compile(
    r"""(?:sk-(?:proj|ant|cp|live|test)-[a-z0-9_-]{12,}|bearer\s+[a-z0-9._-]{12,}|(?:authorization|api[_-]?key|auth[_-]?token|cookie|set-cookie)\s*[:=]\s*["']?[a-z0-9._~+/=-]{12,})""",
    re.I,
)


async def check_evidence(artifacts):
    baseline = await ensure_baseline(artifacts)
    sources, quality = read_json(artifacts.root, SOURCES), read_json(artifacts.root, QUALITY)
    if sources is None or quality is None:
        return {
            "status": "failed",
            "summary": "缺少数据信源渠道或数据质量证据文件。",
            "details": "\n".join(
                relative + " 缺失、超限或不是工作空间内可安全读取的 JSON 文件。"
                for relative, value in ((SOURCES, sources), (QUALITY, quality))
                if value is None
            ),
        }
    source_raw, quality_raw = js_json(sources), js_json(quality)
    if SENSITIVE.search(source_raw + "\n" + quality_raw):
        return {
            "status": "failed",
            "summary": "evidence 文件疑似包含敏感信息。",
            "details": "请移除任何鉴权凭据、会话凭据或密钥值，仅保留数据信源渠道、端点、时间戳和质量摘要。",
        }
    errors, source_entries = [], (record(sources) or {}).get("sources")
    if not isinstance(source_entries, list) or not source_entries:
        errors.append("evidence/sources.json 必须包含非空 sources 数组。")
    if not re.search(
        "source|eastmoney|tencent|endpoint|fetched_at|as_of|quote_time|artifact_path",
        source_raw,
        re.I,
    ):
        errors.append(
            "evidence/sources.json 未检测到 source、endpoint、fetched_at/as_of 或 artifact_path 等来源字段。"
        )
    assessment = assess_evidence(sources, quality)
    errors.extend(assessment["failures"])
    for relative in assessment["artifactPaths"]:
        try:
            if not read_regular(controlled_path(artifacts.root, relative), 8388608):
                errors.append("来源产物为空：" + relative)
        except (OSError, ValueError):
            errors.append("来源产物缺失、超限或路径不安全：" + relative)
    status = (record(quality) or {}).get("status")
    if not isinstance(status, str) or status not in {"ok", "warning", "error"}:
        errors.append("evidence/data_quality.json 必须包含 status，取值为 ok、warning 或 error。")
    if not any_key_deep(
        quality,
        [
            "datasets",
            "checks",
            "missing_fields",
            "warnings",
            "limitations",
            "row_count",
            "fetched_at",
        ],
    ) and not re.search(
        "row_count|missing_fields|warnings|limitations|fetched_at|样本|缺失|限制", quality_raw, re.I
    ):
        errors.append(
            "evidence/data_quality.json 未检测到数据集、检查项、缺失字段、警告或限制说明。"
        )
    if errors:
        return {
            "status": "failed",
            "summary": "数据信源渠道或质量证据不完整。",
            "details": "\n".join(errors),
        }
    warning = status == "warning" or bool(assessment["warnings"])
    return {
        "status": "failed" if status == "error" else "warning" if warning else "passed",
        "summary": "已根据最终数据自动生成数据信源渠道和质量证据文件，状态：" + status + "。"
        if baseline["created"]
        else "数据质量或来源时间存在警告，页面应展示限制说明。"
        if warning
        else "已找到数据信源渠道和质量证据文件。",
        "metadata": {
            "sources": SOURCES,
            "dataQuality": QUALITY,
            "qualityStatus": status,
            "sourceCount": len(source_entries) if isinstance(source_entries, list) else 0,
            "baselineCreated": baseline["created"],
            **({"baselineReason": baseline["reason"]} if "reason" in baseline else {}),
            "evidenceWarnings": assessment["warnings"],
        },
    }


def check_final_data(root):
    try:
        directory = controlled_path(root, "data_file/final")
        if not directory.is_dir():
            return {"status": "failed", "summary": "未找到 data_file/final 目录。"}
        preferred = controlled_path(root, FINAL)
        candidates = (
            [preferred]
            if preferred.is_file()
            else [
                path
                for path in sorted(directory.iterdir())
                if path.suffix == ".json" and path.is_file()
            ]
        )
    except (OSError, ValueError):
        return {"status": "failed", "summary": "未找到 data_file/final 目录。"}
    if not candidates:
        return {
            "status": "failed",
            "summary": "data_file/final 下没有 JSON 数据文件，预期至少生成 dashboard-data.json。",
        }
    errors = []
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        raw = read_text(root, relative)
        if raw is None or len(raw.strip()) <= 2:
            errors.append(relative + " 为空。")
            continue
        try:
            data = read_json(root, relative)
            invalid = [
                assessment
                for assessment in assess_dashboard(data)
                if assessment["status"] == "failed"
            ]
            if invalid:
                errors.append(
                    "行情数据不一致："
                    + "、".join(
                        issue["code"] + "@" + issue["path"]
                        for item in invalid
                        for issue in item["issues"]
                    )
                )
                continue
            plan = read_plan(root)
            planned, fetched, comparison = (
                planned_symbols(plan),
                fetched_symbols(data),
                comparison_symbols(data),
            )
            serialized = js_json(data)
            error = None
            if data is None or data == "" or isinstance(data, (dict, list)) and not data:
                error = " 没有可用数据。"
            elif not re.search(
                "quote|quotes|price|symbol|symbols|assets|comparison|secid|history|kline|financial|reports|announcement|source|fetched_at|quote_time|close|open|volume|amount|backtest|equity_curve|trades|strategy|drawdown|win_rate|营收|净利润|毛利率|roe|回测|净值|回撤|胜率",
                serialized,
                re.I,
            ):
                error = " 未检测到行情、K 线、财务或来源字段。"
            elif re.search(
                "mock|demo|example|placeholder|lorem|示例|样例|模拟|假数据", serialized, re.I
            ):
                error = " 疑似包含示例或模拟数据标记。"
            inspection, empty = inspect_payload(data), empty_screener(data)
            if not error and not inspection["hasUsableMarketData"] and not empty:
                error = " 未提取到可用实时行情或 K 线样本。"
            absent = [symbol for symbol in planned if symbol not in fetched]
            if not error and absent:
                error = " 未覆盖 run_plan 中的全部标的，缺少：" + "、".join(absent) + "。"
            absent = [symbol for symbol in planned if symbol not in comparison]
            if not error and len(planned) > 1 and absent:
                error = " 的 comparison.rows 未覆盖全部对比标的，缺少：" + "、".join(absent) + "。"
            plan_visual = (plan or {}).get("visualization") or {}
            planned_template, expected = (
                pick_string(plan_visual.get("templateId")),
                expected_template(plan),
            )
            value = record(data) or {}
            visual = record(value.get("visualization")) or {}
            final_template = pick_string(
                coalesce(visual.get("template_id"), visual.get("templateId"))
            )
            required = (
                visual["required_components"]
                if isinstance(visual.get("required_components"), list)
                else plan_visual["panels"]
                if isinstance(plan_visual.get("panels"), list)
                else []
            )
            if not error and expected and planned_template != expected:
                error = f" 的任务语义需要 {expected} 模板，但 run_plan.visualization.templateId={planned_template or '未设置'}。"
            if not error and expected and final_template and final_template != expected:
                error = f" 的任务语义需要 {expected} 模板，但 visualization.template_id={final_template}。"
            if not error and planned_template and not final_template:
                error = " 缺少 visualization.template_id，无法验证场景化看板模板。"
            if (
                not error
                and planned_template
                and final_template
                and planned_template != final_template
            ):
                error = f" 的 visualization.template_id={final_template} 与 run_plan={planned_template} 不一致。"
            if not error and planned_template and not required:
                error = " 缺少 visualization.required_components，无法确认页面是否覆盖场景痛点。"
            task = text_for_intent(
                [
                    (plan or {}).get(key)
                    for key in ("question", "task", "instruction", "clarification")
                ]
            )
            trading_rows = (record(value.get("tradingPlan")) or {}).get("rows")
            if (
                not error
                and not trading_plan_intent(task)
                and isinstance(trading_rows, list)
                and trading_rows
            ):
                error = " 包含 tradingPlan.rows，但原始需求没有明确要求交易计划、买入区间、止损或目标价。"

            def rows(key, value=value):
                items = (record(value.get(key)) or {}).get("rows")
                return items if isinstance(items, list) else []

            if not error and planned_template == "stock-selection" and not empty:
                fields = ([] if rows("selectionRanking") else ["selectionRanking.rows"]) + (
                    [] if rows("financialQuality") else ["financialQuality.rows"]
                )
                if any(
                    not isinstance(row, dict)
                    or numeric(row.get("composite_score")) is None
                    or not pick_string(row.get("selection_view"))
                    for row in rows("comparison")
                ):
                    fields.append("comparison.rows[].composite_score/selection_view")
                if fields:
                    error = " 缺少选股模板数据字段：" + "、".join(fields) + "。"
            if not error and planned_template == "holding-analysis":
                fields = (
                    ([] if record(value.get("portfolio")) is not None else ["portfolio"])
                    + (
                        []
                        if isinstance(value.get("holdings"), list) and value["holdings"]
                        else ["holdings[]"]
                    )
                    + (
                        []
                        if isinstance(value.get("assets"), list) and value["assets"]
                        else ["assets[]"]
                    )
                    + ([] if rows("comparison") else ["comparison.rows"])
                )
                if fields:
                    error = " 缺少持仓分析模板数据字段：" + "、".join(fields) + "。"
            if error:
                errors.append(relative + error)
                continue
            return {
                "status": "passed",
                "summary": "已找到可用最终数据文件：" + relative + "。",
                "metadata": {
                    "file": relative,
                    "bytes": len(raw.encode()),
                    "plannedSymbols": planned,
                    "fetchedSymbols": fetched,
                    "comparisonSymbols": comparison,
                    "barCount": inspection["barCount"],
                    "hasQuote": inspection["hasQuote"],
                    "visualizationTemplateId": final_template,
                },
            }
        except (ValueError, TypeError) as cause:
            errors.append(relative + " JSON 解析失败：" + str(cause))
    return {
        "status": "failed",
        "summary": "最终数据文件存在，但没有通过真实数据形态检查。",
        "details": "\n".join(errors),
    }
