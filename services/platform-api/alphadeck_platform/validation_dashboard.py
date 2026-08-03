import re

from .baseline_evidence import FINAL, pick_string
from .finance_values import coalesce, record
from .prefetch_planning import trading_plan_intent
from .provenance import js_json
from .validation_inputs import (
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

TEMPLATE_CHECKS = {
    "holding-analysis": (
        "持仓分析模板",
        [r"持仓|holding|portfolio|仓位|集中度", r"调仓|风险|相关性|流动性|回撤"],
    ),
    "stock-selection": (
        "选股分析模板",
        [
            r"stock-selection|选股|候选|多标的|comparison|assets",
            r"selectionranking|financialquality|排名|相对强弱|研究优先级",
            r"收益对比|波动对比|回撤对比|财务质量|数据口径|更新时间",
        ],
    ),
    "strategy-research": (
        "策略研究模板",
        [
            r"策略假设|可证伪|hypothesis|未回测",
            r"信号规则|筛选规则|候选|comparison|assets",
            r"数据限制|失效风险|风险声明|样本参数",
        ],
    ),
    "single-stock-diagnosis": (
        "个股诊断模板",
        [r"个股|行情|最新价|quote|k\s*线|k线", r"财务|公告|质量|更新时间|报告期"],
    ),
    "technical-timing": (
        "技术择时模板",
        [r"k\s*线|k线|均线|ma20|ma60|成交量", r"触发|失效|趋势|回撤|波动"],
    ),
    "fundamental-research": (
        "基本面研究模板",
        [r"财务|基本面|营收|净利润|roe|毛利率", r"报告期|现金流|公告|估值"],
    ),
    "backtest-review": ("回测复盘模板", [r"回测|净值|策略|胜率|交易", r"参数|回撤|样本|限制"]),
    "sector-rotation": (
        "板块轮动模板",
        [r"板块|行业|指数|etf|轮动|相对强弱", r"收益|回撤|流动性|排名"],
    ),
}


def failed(summary, details=None, metadata=None):
    return {
        "status": "failed",
        "summary": summary,
        **({"details": details} if details is not None else {}),
        **({"metadata": metadata} if metadata is not None else {}),
    }


def check_binding(root):
    page = read_text(root, "src/App.vue")
    if not page:
        return failed("未找到 src/App.vue。")
    if any(
        signal in page
        for signal in ("Get started by editing", "Learn →", "Examples →", "Next.js →")
    ):
        return failed("src/App.vue 仍包含默认页内容。")
    signals = [
        "/api/market",
        "dashboard-data.json",
        "data_file/final",
        "data_file\\final",
        "fetch(",
        "virtual:dashboard-data",
    ]
    binding = [signal for signal in signals if signal in page]
    static = (
        any(
            re.search(pattern, page)
            for pattern in (
                r"const\s+DASHBOARD_DATA\s*[:=]\s*\{",
                r"const\s+(?:STATIC_|MOCK_|SAMPLE_)?(?:QUOTE|QUOTES|HISTORY|KLINE|KLINES|FINANCIALS|REPORTS|ANNOUNCEMENTS|DASHBOARD_DATA)\s*[:=]\s*(?:\[|\{)",
                r"(?:bars|reports|announcements)\s*:\s*\[\s*\{[\s\S]{0,80}(?:open|close|report_date|notice_date|title)\s*:",
            )
        )
        or len(re.findall(r"(?:trade_date|report_date|notice_date|change_percent)\s*:", page)) > 30
    )
    plan, data = read_plan(root), read_json(root, FINAL)
    value = record(data) or {}
    assets = value.get("assets") if isinstance(value.get("assets"), list) else []
    planned, fetched = planned_symbols(plan), fetched_symbols(data)
    inspection = inspect_payload(data)
    plan_visual = (record(plan) or {}).get("visualization") or {}
    template, expected = pick_string(plan_visual.get("templateId")), expected_template(plan)
    task = text_for_intent(
        [(plan or {}).get(key) for key in ("question", "task", "instruction", "clarification")]
    )
    required = [pick_string(panel) for panel in plan_visual.get("panels", []) if pick_string(panel)]
    if not binding:
        return failed(
            "页面未检测到数据文件或同源行情 API 绑定。",
            "src/App.vue 应读取 data_file/final/dashboard-data.json，或通过 /api/market/** 获取真实数据。",
        )
    if static:
        return failed(
            "页面疑似直接硬编码大段行情/财务数据，未形成可复用的数据绑定。",
            "请让 src/App.vue 读取 data_file/final/dashboard-data.json，或通过 /api/market/** 获取数据；不要把完整数据对象内联到页面代码。",
        )
    has_scenario_data = (
        template == "backtest-review"
        and bool((record(value.get("backtest")) or {}).get("equity_curve"))
    ) or (
        template == "fundamental-research"
        and bool((record(value.get("financials")) or {}).get("reports")
                 or (record(value.get("fundamentalIndicators")) or {}).get("points"))
    )
    if not inspection["hasUsableMarketData"] and not has_scenario_data and not empty_screener(data):
        return failed(
            "页面数据入口存在，但最终数据无法映射出实时行情或 K 线样本。",
            "请先生成可用 data_file/final/dashboard-data.json；其中至少应包含 quote.price 或 kline.bars/history.bars 等字段。",
            inspection,
        )
    if not re.search(
        r"function\s+getBars\(|extractBarsFromDashboardData|virtual:dashboard-data|data_file/final/dashboard-data\.json",
        page,
    ):
        return failed(
            "页面未使用 AlphaDeck 标准看板数据绑定结构。",
            "请使用平台标准模板读取 dashboard-data.json，并通过统一解析层渲染最新价、K 线样本、指标、财务和公告。",
        )
    internal = [
        signal
        for signal in ("数据信源渠道", "技术证据", "evidence/sources.json", "场景模板", "必备组件")
        if signal in page
    ]
    if internal:
        return failed(
            "页面把后台审计或生成契约信息渲染到了用户看板。",
            "请移除用户可见的 "
            + "、".join(internal)
            + " 分区；页面只保留更新时间、报告期、样本口径和质量/缺失提示，渠道端点、技术路径、模板 ID 与组件契约继续保留在后台 evidence/run plan 中。",
            {"internalPresentationSignals": internal},
        )
    if expected and template != expected:
        guidance = {
            "holding-analysis": "组合、持仓、调仓或账户类任务必须走持仓分析模板。",
            "stock-selection": "多标的比较或选股任务必须走候选对比模板。",
            "strategy-research": "策略假设与筛选研究必须走策略研究模板。",
            "technical-timing": "技术分析任务必须走技术择时模板。",
            "fundamental-research": "基本面分析任务必须走基本面研究模板。",
        }
        return failed(
            f"执行计划模板与任务语义不一致，应使用 {expected}。",
            f"当前 run_plan.visualization.templateId={template or '未设置'}。"
            + guidance.get(expected, "请按 capability 任务合同选择场景模板。"),
            {"expectedTemplateId": expected, "plannedTemplateId": template},
        )
    visual = record(value.get("visualization")) or {}
    final = pick_string(coalesce(visual.get("template_id"), visual.get("templateId")))
    if expected and final and final != expected:
        return failed(
            f"最终数据模板与任务语义不一致，应使用 {expected}。",
            f"当前 data_file/final/dashboard-data.json visualization.template_id={final}。",
            {"expectedTemplateId": expected, "finalTemplateId": final},
        )
    trading_rows = (record(value.get("tradingPlan")) or {}).get("rows")
    has_trading = isinstance(trading_rows, list) and bool(trading_rows)
    page_trading = bool(
        re.search(
            "短线交易计划|交易计划|买入区间|买点|卖点|止损|止盈|目标价|仓位上限|入场|出场", page
        )
    )
    if not trading_plan_intent(task) and (has_trading or page_trading):
        return failed(
            "页面包含未被用户明确要求的交易执行计划。",
            "原始需求没有要求买入区间、止损、目标价、仓位或操作建议。请移除 tradingPlan 和页面中的短线交易计划，只保留事实对比、研究结论、风险提示和数据限制。",
            {"hasTradingPlanData": has_trading, "hasPageTradingPlan": page_trading},
        )
    if len(planned) > 1 or len(assets) > 1:
        coverage = re.search("requestedSymbols|assets|comparison", page) and all(
            symbol in fetched for symbol in planned
        )
        absent = [] if coverage else [symbol for symbol in planned if symbol not in page]
        comparison = bool(
            re.search(
                "assets|comparison|requestedSymbols|assetCount|对比|相对强弱|多标的|收益对比|回撤对比|波动",
                page,
            )
        )
        if absent or not comparison:
            details = (["页面未显式覆盖标的：" + "、".join(absent) + "。"] if absent else []) + (
                [] if comparison else ["页面未检测到 assets[]、comparison 或多标的对比展示逻辑。"]
            )
            return failed(
                "页面未完整绑定多标的对比数据。",
                "\n".join(details),
                {"plannedSymbols": planned, "fetchedSymbols": fetched, "assetCount": len(assets)},
            )
    if template:
        patterns = TEMPLATE_CHECKS.get(template)
        serialized = js_json(data if data is not None else {}).lower()
        absent = (
            [
                pattern
                for pattern in patterns[1]
                if not re.search(pattern, page)
                and not re.search(pattern, page.lower())
                and not re.search(pattern, serialized)
            ]
            if patterns
            else []
        )
        if patterns and absent:
            return failed(
                "页面未体现 " + patterns[0] + " 的关键组件。",
                "\n".join(
                    [
                        "run_plan.visualization.templateId=" + template,
                        *(["必备组件：" + "、".join(required)] if required else []),
                        "缺少信号：" + "；".join(absent),
                    ]
                ),
            )
        if template == "holding-analysis" and re.search(
            r"hero-band|risk-card|holding-analysis\s*持仓分析模板|持仓问题快速诊断", page, re.I
        ):
            return failed(
                "持仓分析页面仍使用过重的顶部 hero 结构。",
                "持仓、调仓和截图账户类看板应直接从账户摘要、持仓矩阵或核心风险指标开始；VaR、样本口径和声明应放入连续指标带、风险分区或底部说明，不要占据首屏顶部。",
            )
        # Shared Vue renderers contain conditional sections for several scenarios.
        # A holding branch in source code does not mean it appears on a selection page.
    return {
        "status": "passed",
        "summary": "页面已检测到真实数据绑定入口。",
        "metadata": {"signals": binding},
    }


def check_charts(root):
    page = read_text(root, "src/App.vue")
    if not page:
        return failed("未找到 src/App.vue，无法检查图表。")
    visual = "\n".join(
        [
            page,
            *(
                value
                for path in ("src/style.css", "styles/globals.css")
                if (value := read_text(root, path))
            ),
        ]
    )
    graphic = bool(
        re.search(
            r"<svg|<canvas|<polyline|<rect|<path|Chart|chart|candlestick|ohlc|K线|K 线|折线|柱状|趋势图",
            page,
            re.I,
        )
    )
    finance = bool(
        re.search(
            r"成交量|成交额|均线|MA5|MA10|MA20|K线|K 线|营收|净利润|ROE|毛利率|回撤|波动率|quote|history|financial",
            page,
            re.I,
        )
    )
    coloring = bool(
        re.search(
            r"red|green|up|down|gain|loss|risk-(?:high|mid|low)|dot\s+(?:red|green|amber)|candle-up|candle-down|volume-up|volume-down|bar-up|bar-down|quality-(?:ok|warning|error)|signal-(?:up|down)|#d9363e|#15945b|#dc2626|#16a34a",
            visual,
            re.I,
        )
    )
    aid = bool(
        re.search(
            r"<title>|<desc>|aria-label|chart-label|axis|grid|legend|tooltip|刻度|图例|坐标|日期",
            page,
            re.I,
        )
    )
    mini = re.search(
        r"""className="(?:sparkline|mini-kline)"|className='(?:sparkline|mini-kline)'|sparkline-empty|MiniKlineChart""",
        page,
        re.I,
    ) and not re.search(
        r"chart-label|chart-price|chart-date|volume-chart|KLinePanel|MainKline|主图|成交量副图",
        page,
        re.I,
    )
    plan = read_plan(root)
    planned = planned_symbols(plan)
    raw = read_text(root, FINAL)
    multi = len(planned) > 1 or raw and re.search(r'"assets"\s*:|"comparison"\s*:', raw)
    template = pick_string((record((plan or {}).get("visualization")) or {}).get("templateId"))
    if not graphic or not finance:
        return failed(
            "未检测到有效金融图表实现。",
            "页面至少应包含 SVG/canvas/图表组件，并展示 K 线、成交量、均线、财务趋势或风险指标。",
        )
    if not coloring or not aid:
        return failed(
            "金融图表缺少语义染色或读图辅助。",
            "页面需要为涨跌、风险、质量状态提供明确颜色，并给 SVG/canvas 图表提供坐标/图例/tooltip/title 等读图辅助。",
            {"hasSemanticColoring": coloring, "hasChartReadingAid": aid},
        )
    if mini:
        return failed(
            "金融图表只有迷你趋势图，缺少可读主图。",
            "多标的页面可以保留 sparkline，但必须额外提供带坐标/日期/图例/成交量或对比尺度的主图、矩阵或表格。",
            {"plannedSymbols": planned, "plannedTemplateId": template},
        )
    if multi and not re.search(
        "对比|相对强弱|多标的|矩阵|收益|波动|回撤|comparison|assets", page, re.I
    ):
        return failed(
            "多标的任务未检测到对比图表或对比指标展示。",
            "页面需要展示多标的指标矩阵、收益对比、波动/回撤对比或相对强弱摘要。",
            {"plannedSymbols": planned},
        )
    if template == "stock-selection" and not re.search(
        "selectionRanking|financialQuality|stock-selection|相对强弱与排名依据|财务质量|收益对比图|波动对比图|回撤对比图",
        page,
    ):
        return failed(
            "选股任务未检测到场景化选股图表组件。",
            "页面需要展示相对强弱/排名依据、财务质量、收益对比图、波动对比图或回撤对比图。",
            {"plannedSymbols": planned, "plannedTemplateId": template},
        )
    if template == "technical-timing":
        ma60 = bool(
            re.search(
                r"""legend-ma60|className=["'][^"']*ma60|(?:ma60|MA60)[\w]*\s*\.map\(|name\s*:\s*["']MA60""",
                page,
                re.I,
            )
        )
        risk, volume = (
            bool(re.search("风险结论|风险等级", page)),
            bool(re.search("volume-chart|成交量副图|VolumeChart|volumeBars", page)),
        )
        if not ma60 or not risk or not volume:
            return failed(
                "技术择时看板缺少完整的 MA60、成交量或风险结论。",
                "\n".join(
                    ([] if ma60 else ["MA60 必须实际绘制到主图，不能只出现在文字或组件清单中。"])
                    + ([] if volume else ["必须绘制成交量副图。"])
                    + ([] if risk else ["必须显式展示风险结论或风险等级。"])
                ),
                {
                    "hasMa60Graphic": ma60,
                    "hasVolumeGraphic": volume,
                    "hasExplicitRiskConclusion": risk,
                    "plannedTemplateId": template,
                },
            )
    return {"status": "passed", "summary": "已检测到金融图表相关实现。"}
