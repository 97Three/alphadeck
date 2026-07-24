import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .finance_values import record

CAPABILITIES = frozenset(
    (
        "stock_diagnosis",
        "technical_analysis",
        "fundamental_analysis",
        "asset_comparison",
        "sector_rotation",
        "strategy_research",
        "backtest_review",
        "portfolio_risk",
    )
)
SYMBOL = re.compile(r"[6035][0-9]{5}\Z")


def is_quant_plan(plan):
    return plan.get("capabilityId") in CAPABILITIES


def trading_plan_intent(instruction):
    return bool(
        re.search(
            r"交易计划|买入区间|买点|卖点|入场|出场|止损|止盈|目标价|仓位|建仓|加仓|减仓|卖出|买入|(?:要|想|准备)买|推荐.*(?:买|交易)|怎么操作|如何操作|操作建议|短线.*(?:买|卖|交易|计划)|(?:1|3|5|一|三|五)个交易日.*(?:计划|操作)|持仓.*(?:调仓|减仓|加仓)",
            re.sub(r"\s+", "", instruction),
        )
    )


def unique_symbols(values):
    return list(
        dict.fromkeys(
            value for value in values if isinstance(value, str) and SYMBOL.fullmatch(value)
        )
    )


def pick_symbol(value):
    if isinstance(value, str):
        return value.strip() if SYMBOL.fullmatch(value.strip()) else None
    value = record(value) or {}
    for candidate in [
        *(value.get(key) for key in ("symbol", "code", "security_code", "securityCode", "ticker")),
        value.get("secid", "").split(".")[-1] if isinstance(value.get("secid"), str) else None,
    ]:
        if isinstance(candidate, str) and SYMBOL.fullmatch(candidate.strip()):
            return candidate.strip()
    return None


def planned_symbols(plan):
    return unique_symbols(map(pick_symbol, plan.get("symbols") or []))[:8]


def broad_screener(plan):
    rewrite = plan.get("queryRewrite")
    if rewrite and not rewrite.get("broadUniverse"):
        return False
    return any(
        "/api/v1/research/screeners/a-share/short-term-candidates" in endpoint
        for endpoint in plan.get("dataRequirements", [])
    )


def candidate_code(value):
    value = record(value) or {}
    for key in ("code", "symbol", "security_code", "securityCode"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            match = re.search(
                r"(?:^|[^0-9])([6035][0-9]{5})(?:\.(?:SH|SZ|BJ))?$", candidate.strip(), re.I
            ) or re.match(r"([6035][0-9]{5})(?:\.(?:SH|SZ|BJ))?", candidate.strip(), re.I)
            if match:
                return match[1]
    return None


def screener_mode(question):
    question = re.sub(r"\s+", "", question)
    return (
        "limit_up_relay"
        if re.search("涨停|连板|接力", question)
        else "trend_liquidity"
        if re.search("趋势|强弱|流动性|均线", question)
        else "short_term"
    )


def screener_limit(question):
    match = re.search(r"(?:候选|筛选|推荐|选出)?([0-9]+)(?:只|个)", re.sub(r"\s+", "", question))
    # JS parseInt overflows to Infinity and falls back to five.
    return 5 if match and len(match[1]) > 308 else min(max(int(match[1]) if match else 5, 3), 10)


def screener_trade_date(question, year=None):
    question = re.sub(r"\s+", "", question)
    match = re.search(r"((?:19|20)[0-9]{2})[-年/]?([0-9]{1,2})[-月/]([0-9]{1,2})日?", question)
    if match:
        parts = tuple(map(int, match.groups()))
    else:
        match = re.search(r"([0-9]{1,2})月([0-9]{1,2})日?", question)
        if not match:
            return None
        parts = (year or datetime.now(ZoneInfo("Asia/Shanghai")).year, *map(int, match.groups()))
    try:
        return date(*parts).isoformat()
    except ValueError:
        return None


def history_limit(plan):
    accepted = (plan.get("queryRewrite") or {}).get("timeRange") or {}
    value = accepted.get("value")
    multipliers = {"trading_day": 1, "day": 1, "week": 5, "month": 21, "quarter": 63, "year": 252}
    if (
        type(value) in {int, float}
        and value > 0
        and value <= 9007199254740991
        and value == int(value)
        and accepted.get("unit") in multipliers
    ):
        return int(min(max(value * multipliers[accepted["unit"]], 20), 500))
    source = re.sub(r"\s+", "", accepted.get("label") or plan.get("timeRange") or "")
    match = re.search(r"最近([0-9]+)(?:个)?(?:交易日|日|天)", source)
    if match:
        return 120 if len(match[1]) > 308 else min(max(int(match[1]), 20), 500)
    for pattern, days in (
        (r"近?两年|最近两年|过去两年|2年|24个月", 500),
        (r"近?一年|最近一年|过去一年|1年|12个月|十二个月", 252),
        (r"近?半年|最近半年|过去半年|6个月|六个月", 126),
        (r"(?:去年|今年)?(?:上半年|下半年)", 126),
        (r"年初至今", 252),
        (r"近?三个月|最近三个月|过去三个月|3个月|一季度|一个季度", 63),
    ):
        if re.search(pattern, source):
            return days
    return 120


async def sync_plan_symbols(artifacts, plan, symbols, source, warnings, now):
    symbols = unique_symbols(symbols)
    if not symbols and source != "screener":
        return
    current = artifacts.read(".data-agent/finance-run-plan.json")
    if current is None:
        return
    previous = unique_symbols(map(pick_symbol, current.get("symbols") or []))
    plan["symbols"] = symbols
    if previous == symbols and record(current.get("symbolResolution")) is not None:
        return
    await artifacts.write(
        ".data-agent/finance-run-plan.json",
        {
            **current,
            "symbols": symbols,
            "symbolResolution": {"source": source, "resolvedAt": now, "warnings": warnings},
            "updatedAt": now,
        },
    )
