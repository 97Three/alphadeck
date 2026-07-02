import asyncio
import json
import math
import os
import re

from .config import ROOT
from .evaluation_scoring import number, record, score
from .http_input import reject_constant
from .llm_config import project_llm_config
from .messages import js_length, js_slice
from .provenance import js_json
from .provider import ChatProvider
from .review_evidence import load_evidence, resolves
from .settings import DEFAULT_MODEL

PROMPT_VERSION = "alphadeck-agent-review-prompt-v2"
REVIEW_DIMENSIONS = (
    "intentCoverage",
    "businessCompleteness",
    "grounding",
    "riskCommunication",
    "actionability",
)


def unique_count(values):
    # JS Set compares object references, and primitive values by their typed identity.
    return len(
        {
            ("object", id(value)) if isinstance(value, (dict, list)) else (type(value), value)
            for value in values
        }
    )


def parse_review(value, usage=None, reviewer=None, evidence=None):
    stripped = value.strip()
    if match := re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, re.I):
        stripped = match[1].strip()
    parsed = record(json.loads(stripped, parse_constant=reject_constant))
    dimensions_raw = parsed.get("dimensions")
    dimensions_raw = (
        [record(item) for item in dimensions_raw] if isinstance(dimensions_raw, list) else []
    )
    issues = []
    if evidence is None:
        issues.append("evidence_catalog_missing")
    raw_ids = [item.get("id") for item in dimensions_raw]
    valid_rubric = (
        len(raw_ids) == len(REVIEW_DIMENSIONS)
        and unique_count(raw_ids) == len(REVIEW_DIMENSIONS)
        and all(identity in REVIEW_DIMENSIONS for identity in raw_ids)
    )
    if not valid_rubric:
        issues.append("rubric_dimensions_invalid")
    dimensions = []
    for identity in REVIEW_DIMENSIONS:
        raw = next((item for item in dimensions_raw if item.get("id") == identity), {})
        value_score = number(raw.get("score"), None)
        if value_score is None or int(value_score) != value_score or not 0 <= value_score <= 100:
            issues.append(identity + ":score_invalid")
        if not isinstance(raw.get("rationale"), str) or not raw["rationale"].strip():
            issues.append(identity + ":rationale_missing")
        references = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
        valid_shape = (
            isinstance(raw.get("evidence"), list)
            and len(references) <= 8
            and unique_count(references) == len(references)
        )
        if not valid_shape:
            issues.append(identity + ":citation_format_invalid")
        valid = [
            reference
            for reference in references
            if isinstance(reference, str) and evidence is not None and resolves(reference, evidence)
        ]
        if len(valid) != len(references):
            issues.append(identity + ":citation_invalid")
        if score(raw.get("score")) > 0 and not valid:
            issues.append(identity + ":citation_missing")
        dimensions.append(
            {
                "id": identity,
                "score": score(raw.get("score"))
                if valid_rubric
                and valid_shape
                and not any(issue.startswith(identity + ":") for issue in issues)
                else 0,
                "rationale": js_slice(raw["rationale"], 0, 1000)
                if isinstance(raw.get("rationale"), str)
                else "未提供评价依据。",
                "evidence": valid[:8],
            }
        )
    total = math.floor(sum(item["score"] for item in dimensions) / len(dimensions) + 0.5)
    by_id = {item["id"]: item["score"] for item in dimensions}
    truncated = [identity for identity, item in (evidence or {}).items() if item["truncated"]]
    grounding, risk = by_id["grounding"], by_id["riskCommunication"]
    verdict = (
        "failed"
        if issues or total < 70 or grounding < 60 or risk < 60
        else "warning"
        if truncated or total < 85 or grounding < 75 or risk < 75
        else "passed"
    )
    return {
        "schemaVersion": 1,
        "reviewer": {
            **(reviewer or {"provider": "openai", "model": DEFAULT_MODEL}),
            "promptVersion": PROMPT_VERSION,
            "independentFromGenerator": False,
        },
        "verdict": verdict,
        "score": total,
        "summary": js_slice(parsed["summary"], 0, 1500)
        if isinstance(parsed.get("summary"), str)
        else f"语义审阅综合得分 {total}。",
        "dimensions": dimensions,
        "evidenceValidation": {
            "status": "failed" if issues else "verified",
            "issues": issues[:32],
            "artifactHashes": {
                identity: item["sha256"] for identity, item in (evidence or {}).items()
            },
            "truncatedArtifacts": truncated,
        },
        "usage": {key: usage[key] for key in ("inputTokens", "outputTokens", "totalTokens")}
        if usage
        else None,
    }


async def review_workspace(
    root, question, test_case, deterministic_result, *, api_key=None, model=None, provider=None
):
    config = project_llm_config(model or DEFAULT_MODEL)
    key = (api_key or os.getenv(config["credentialEnv"], "")).strip()
    if not key and provider is None:
        raise ValueError("Agent 语义审阅需要 " + config["credentialEnv"] + "。")
    artifacts = load_evidence(root)
    checks = record(record(deterministic_result).get("validation")).get("checks")
    evidence = {
        "question": js_slice(question, 0, 16000),
        "questionTruncated": js_length(question) > 16000,
        "expectedContract": {
            key: test_case[key]
            for key in (
                "id",
                "capabilityId",
                "expectedSymbol",
                "expectedSymbols",
                "expectedFinalFields",
                "expectedTemplateId",
            )
            if key in test_case
        },
        "deterministicResult": {
            "passed": record(deterministic_result).get("passed") is True,
            "checks": [
                {key: record(item)[key] for key in ("id", "status") if key in record(item)}
                for item in checks[:64]
            ]
            if isinstance(checks, list)
            else [],
        },
        "artifacts": artifacts,
    }
    system = json.loads((ROOT / "config/evaluation-catalog.json").read_text())["reviewSystemPrompt"]
    stream = provider or ChatProvider(
        key,
        config["baseUrl"],
        deepseek=config["provider"] == "deepseek",
        max_retries=1,
        limits={"maxTextChars": 20000},
    )
    text, usage, completed = "", None, False
    async with asyncio.timeout(60):
        async for event in stream.complete(
            {
                "model": config["model"],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": js_json(evidence)},
                ],
                "toolChoice": "none",
                "temperature": 0,
                "maxTokens": 2000,
                "reasoning": {"enabled": False},
                "metadata": {"purpose": "alphadeck-agent-evaluation"},
            }
        ):
            if event["type"] == "finish":
                if event["reason"] != "stop":
                    raise ValueError("Agent 语义审阅未完整结束，拒绝使用部分评分。")
                completed = True
            if event["type"] == "text_delta":
                text += event["delta"]
            if event["type"] == "usage":
                usage = event["usage"]
    if not completed:
        raise ValueError("Agent 语义审阅缺少完成信号。")
    if not text.strip():
        raise ValueError("Agent 语义审阅没有返回 JSON。")
    return parse_review(
        text, usage, {"provider": config["provider"], "model": config["model"]}, artifacts
    )
