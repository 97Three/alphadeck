import re
from datetime import UTC, date, datetime, timedelta


class ResearchTimeError(ValueError):
    code = "QUANT_RESEARCH_TIME_UNSUPPORTED"


def calendar_day(value):
    try:
        return (
            isinstance(value, str)
            and bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
            and date.fromisoformat(value).isoformat() == value
        )
    except ValueError:
        return False


def parse_timestamp(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,3})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
            value,
        )
        or not calendar_day(value[:10])
    ):
        raise ResearchTimeError("研究截止时点必须是带时区的有效时间。")
    try:
        if value[11:19] == "24:00:00" and not re.search(r"\.\d*[1-9]", value):
            return datetime.fromisoformat(
                (value[:11] + "00:00:00" + value[19:]).replace("Z", "+00:00")
            ) + timedelta(days=1)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as cause:
        raise ResearchTimeError("研究截止时点必须是带时区的有效时间。") from cause


def resolve_research_time(value, now=None):
    if not value:
        return None
    start, end, cutoff = value.get("startDate"), value.get("endDate"), value.get("asOf")
    if not start and not end and not cutoff and value.get("unit") != "date_range":
        return None
    if not end or not calendar_day(end) or (start and not calendar_day(start)):
        raise ResearchTimeError("历史研究必须提供有效的结构化结束日期；请重新规划时间范围。")
    cutoff = cutoff if cutoff is not None else end + "T23:59:59.999+08:00"
    parsed = parse_timestamp(cutoff).astimezone(UTC)
    now = now or datetime.now(UTC)
    now = datetime.fromisoformat(now.replace("Z", "+00:00")) if isinstance(now, str) else now
    if parsed > now:
        raise ResearchTimeError("研究截止时点尚未发生，不能读取未来数据。")
    local = parsed + timedelta(hours=8)
    cutoff_day = local.date().isoformat()
    if end > cutoff_day or (start and start > end):
        raise ResearchTimeError("研究日期顺序无效，或结束日期晚于截止时点。")
    complete = (
        cutoff_day
        if (local.hour, local.minute, local.second, local.microsecond) == (23, 59, 59, 999000)
        else (local - timedelta(days=1)).date().isoformat()
    )
    end = min(end, complete)
    if start and (
        start > end or (date.fromisoformat(end) - date.fromisoformat(start)).days >= 1000
    ):
        raise ResearchTimeError("当前历史日线范围须包含完整日期，且不超过 1,000 个自然日。")
    return {
        "schemaVersion": 1,
        "startDate": start if start is not None else None,
        "endDate": end,
        "asOf": parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "timezone": "Asia/Shanghai",
    }


def research_time_for_plan(plan, now=None):
    persisted = plan.get("researchTime")
    if persisted:
        if (
            type(persisted.get("schemaVersion")) is not int
            or persisted["schemaVersion"] != 1
            or persisted.get("timezone") != "Asia/Shanghai"
        ):
            raise ResearchTimeError("研究时间合同版本或时区无效。")
        return resolve_research_time({**persisted, "unit": "date_range"}, now)
    return resolve_research_time((plan.get("queryRewrite") or {}).get("timeRange"), now)


def constrain_historical_query(path, query, time):
    if not time:
        return query
    result = dict(query)

    def require(key, value):
        if key in result and result[key] != value:
            raise ResearchTimeError(f"接口参数 {key} 与研究时间合同不一致。")
        result[key] = value

    if re.fullmatch(r"/api/v1/(?:fundamentals/financials|indicators/fundamental)/[^/]+", path):
        require("as_of", time["asOf"])
    elif re.fullmatch(
        r"/api/v1/(?:quotes/history|indicators/technical|backtests/(?:ma-crossover|strategies/[^/]+))/[^/]+",
        path,
    ):
        require("end", time["endDate"].replace("-", ""))
        if time["startDate"]:
            require("start", time["startDate"].replace("-", ""))
        require("period", "daily")
        require("adjustment", "none")
    elif path == "/api/v1/research/screeners/a-share/short-term-candidates":
        require("trade_date", time["endDate"])
    elif path not in {"/api/v1/registry", "/api/v1/symbols/resolve"}:
        raise ResearchTimeError("此接口尚未提供历史时点读取，不能用于当前历史研究。")
    return result
