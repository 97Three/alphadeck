import json
import math
import os
import re
from copy import deepcopy

from .llm_config import project_llm_config
from .messages import js_length
from .provenance import js_json
from .provider import ChatProvider, ProviderError
from .query_rewrite import CATALOG, FOCUS, UNITS


class SemanticSchemaError(ValueError):
    def __init__(self, field, code):
        self.field, self.code = field, code
        super().__init__(field + ":" + code)


def schema_error(field, code):
    raise SemanticSchemaError(field, code)


def nullable_literal(value):
    return (
        None if isinstance(value, str) and value.strip().lower() in {"", "null", "none"} else value
    )


def text(value, field, maximum, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        schema_error(field, "invalid_type")
    value = value.strip()
    if not value:
        schema_error(field, "too_small")
    if js_length(value) > maximum:
        schema_error(field, "too_big")
    return value


def parse_semantics(payload):
    if not isinstance(payload, dict):
        schema_error("<root>", "invalid_type")
    allowed = {
        "targetCandidates",
        "timeRange",
        "analysisFocusId",
        "outputIntent",
        "answerOnlyEvidence",
        "broadUniverse",
        "broadUniverseEvidence",
        "confidence",
    }
    if payload.keys() - allowed:
        schema_error("<root>", "unrecognized_keys")
    result = deepcopy(payload)
    targets = payload.get("targetCandidates")
    if not isinstance(targets, list):
        schema_error("targetCandidates", "invalid_type")
    if len(targets) > 8:
        schema_error("targetCandidates", "too_big")
    result["targetCandidates"] = [
        text(value, "targetCandidates." + str(index), 24) for index, value in enumerate(targets)
    ]
    if "timeRange" not in payload:
        schema_error("timeRange", "invalid_type")
    temporal = nullable_literal(payload["timeRange"])
    if temporal is not None:
        if not isinstance(temporal, dict):
            schema_error("timeRange", "invalid_type")
        if temporal.keys() - {"label", "value", "unit", "startDate", "endDate", "asOf", "evidence"}:
            schema_error("timeRange", "unrecognized_keys")
        temporal = dict(temporal)
        temporal["label"] = text(temporal.get("label"), "timeRange.label", 64)
        temporal["evidence"] = text(temporal.get("evidence"), "timeRange.evidence", 160)
        if not isinstance(temporal.get("unit"), str) or temporal["unit"] not in UNITS:
            schema_error("timeRange.unit", "invalid_value")
        value = nullable_literal(temporal.get("value"))
        if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            value = int(value)
        if value is None:
            temporal.pop("value", None)
        else:
            if type(value) not in {int, float} or not math.isfinite(value) or int(value) != value:
                schema_error("timeRange.value", "invalid_type")
            if not 1 <= value <= 5000:
                schema_error("timeRange.value", "too_small" if value < 1 else "too_big")
            temporal["value"] = int(value)
        for key in ("startDate", "endDate", "asOf"):
            if temporal.get(key) is None:
                temporal.pop(key, None)
            elif not isinstance(temporal[key], str):
                schema_error("timeRange." + key, "invalid_type")
    result["timeRange"] = temporal
    for key in ("answerOnlyEvidence", "broadUniverseEvidence"):
        result[key] = text(nullable_literal(payload.get(key)), key, 160, nullable=True)
    if not isinstance(result.get("analysisFocusId"), str) or result["analysisFocusId"] not in FOCUS:
        schema_error("analysisFocusId", "invalid_value")
    if result.get("outputIntent") not in ("dashboard", "answer"):
        schema_error("outputIntent", "invalid_value")
    if type(result.get("broadUniverse")) is not bool:
        schema_error("broadUniverse", "invalid_type")
    confidence = result.get("confidence")
    if type(confidence) not in {int, float} or not math.isfinite(confidence):
        schema_error("confidence", "invalid_type")
    if not 0 <= confidence <= 1:
        schema_error("confidence", "too_small" if confidence < 0 else "too_big")
    return result


async def rewrite_with_provider(input, provider, *, model=None, provider_name=None, repair=None):
    model = project_llm_config(model or input.get("requestedModel"))["model"]
    name = provider_name or getattr(
        provider, "name", "deepseek" if getattr(provider, "deepseek", False) else "openai"
    )
    request = deepcopy(CATALOG["request"])
    request["model"] = model
    request.pop("signal", None)
    if repair:
        request["messages"][0]["content"] += " Schema repair for this retry: " + repair
    prompt = {
        **CATALOG["prompt"],
        "referenceTime": input["referenceTime"],
        "normalizedQuery": input["normalizedQuery"],
        "executionRole": input["trigger"],
    }
    request["messages"][1]["content"] = js_json(prompt)
    tool_name, arguments, usage = "", "", None
    async for event in provider.complete(request):
        if event["type"] == "tool_call_delta" and event["index"] == 0:
            tool_name += event.get("nameDelta") or ""
            arguments += event.get("argumentsDelta") or ""
        elif event["type"] == "usage":
            usage = event["usage"]
    tool = request["tools"][0]["name"]

    def invalid(instruction):
        return {
            "ok": False,
            "code": "LLM_INVALID_OUTPUT",
            "provider": name,
            "model": model,
            "retryable": False,
            "repairInstruction": instruction,
        }

    if tool_name != tool or not arguments.strip():
        return invalid(
            f"The previous response did not call {tool} exactly once with non-empty arguments. Call that tool exactly once and emit the complete object."
        )
    try:
        payload = json.loads(
            arguments,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid constant")),
        )
    except ValueError:
        return invalid(
            "The previous tool arguments were not valid JSON. Emit one complete JSON object through the required tool; do not emit prose or partial JSON."
        )
    try:
        parsed = parse_semantics(payload)
    except SemanticSchemaError as cause:
        return invalid(
            f"The previous tool object failed the declared schema at {cause.field}:{cause.code}. Emit the complete object again, using only declared fields and exact enum/JSON types."
        )
    return {
        "ok": True,
        "data": parsed,
        "provider": name,
        "model": model,
        **(
            {"usage": {key: usage[key] for key in ("inputTokens", "outputTokens", "totalTokens")}}
            if usage
            else {}
        ),
    }


def nonnegative_env(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value.strip() or "0")
        return (
            int(parsed)
            if math.isfinite(parsed) and parsed.is_integer() and 0 <= parsed <= 9007199254740991
            else default
        )
    except ValueError:
        return default


def modelport_headers():
    result = {}
    for key, default, header in (
        ("ALPHADECK_MODELPORT_ORGANIZATION_ID", "org_local", "Organization"),
        ("ALPHADECK_MODELPORT_PROJECT_ID", "prj_alphadeck", "Project"),
        (
            "ALPHADECK_MODELPORT_ENVIRONMENT_ID",
            "env_production" if os.getenv("NODE_ENV") == "production" else "env_development",
            "Environment",
        ),
    ):
        value = os.getenv(key, "").strip() or default
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise ValueError("Invalid ModelPort scope identifier")
        result["X-ModelPort-" + header + "-Id"] = value
    return result


async def configured_rewriter(input, *, client=None):
    config = project_llm_config(input.get("requestedModel"))
    model, name = config["model"], config["provider"]
    key = os.getenv(config["credentialEnv"], "").strip()
    if not config["queryRewrite"]["enabled"] or not key:
        return {
            "ok": False,
            "code": "LLM_NOT_CONFIGURED",
            "provider": name,
            "model": model,
            "retryable": False,
        }
    limits = {"maxToolCalls": 1}
    for field, suffix, default in (
        ("maxRequestBytes", "MAX_REQUEST_BYTES", 32000),
        ("maxTextChars", "MAX_TEXT_CHARS", 4000),
        ("maxReasoningChars", "MAX_REASONING_CHARS", 4000),
        ("maxToolArgumentChars", "MAX_TOOL_ARGUMENT_CHARS", 8000),
    ):
        limits[field] = max(
            1, nonnegative_env("ALPHADECK_QUERY_REWRITE_LLM_" + suffix, default) or default
        )
    provider = ChatProvider(
        key,
        config["baseUrl"],
        deepseek=name == "deepseek",
        headers={
            "X-Client-App": "AlphaDeck-Query-Rewrite/4",
            **(modelport_headers() if name == "openai" else {}),
        },
        limits=limits,
        max_retries=min(
            1,
            nonnegative_env(
                "ALPHADECK_QUERY_REWRITE_LLM_MAX_RETRIES", config["queryRewrite"]["maxRetries"]
            ),
        ),
        initial_delay_ms=250,
        max_delay_ms=1000,
        client=client,
    )
    try:
        repair = None
        attempts = 1 + min(
            2,
            nonnegative_env(
                "ALPHADECK_QUERY_REWRITE_LLM_INVALID_OUTPUT_RETRIES",
                2 if model == "local_qwen:qwen3.5-9b-q5km" else 0,
            ),
        )
        for _ in range(attempts):
            outcome = await rewrite_with_provider(
                input, provider, model=model, provider_name=name, repair=repair
            )
            if outcome["ok"] or outcome["code"] != "LLM_INVALID_OUTPUT":
                return outcome
            repair = outcome.get("repairInstruction")
        return outcome
    except ProviderError as cause:
        retryable = (
            cause.code == "NETWORK_ERROR"
            or cause.status in {408, 409, 425, 429}
            or (cause.status is not None and cause.status >= 500)
        )
        return {
            "ok": False,
            "code": "LLM_" + cause.code,
            "provider": name,
            "model": model,
            "retryable": retryable,
        }
    except TimeoutError:
        return {
            "ok": False,
            "code": "LLM_TIMEOUT",
            "provider": name,
            "model": model,
            "retryable": True,
        }
    except Exception:
        return {
            "ok": False,
            "code": "LLM_REWRITE_FAILED",
            "provider": name,
            "model": model,
            "retryable": True,
        }
