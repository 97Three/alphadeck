import argparse
import hashlib
import hmac
import json
import os
import re
import unicodedata
from copy import deepcopy
from pathlib import Path

from .finance_values import js_string
from .messages import js_length
from .skill_integrity import read_regular
from .skill_source import strict_json
from .workspace import write_atomic

PATTERNS = (
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I | re.ASCII), "[EMAIL]"),
    ("mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)", re.ASCII), "[MOBILE]"),
    ("cn_id", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)", re.ASCII), "[CN_ID]"),
    ("long_account", re.compile(r"(?<!\d)\d{12,19}(?!\d)", re.ASCII), "[ACCOUNT]"),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", re.ASCII), "[IP]"),
)
ALLOWED = (
    "capabilityId",
    "type",
    "expectedSymbol",
    "expectedSymbols",
    "expectedAssetType",
    "expectedTemplateId",
    "expectedVariantId",
    "expectedDatasets",
    "expectedRawFiles",
    "expectedFinalFields",
    "oracleAssertions",
    "safetyTags",
    "expectClarification",
    "visualCheck",
)


def redact(value, key):
    if js_length(key) < 16:
        raise ValueError("生产回放 hash key 至少需要 16 个字符")
    source = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", js_string(value if value is not None else "")).strip(),
    )
    text, kinds = source, []
    for kind, pattern, replacement in PATTERNS:
        if pattern.search(text):
            kinds.append(kind)
            text = pattern.sub(replacement, text)
    return {
        "text": text,
        "sourcePromptHmacSha256": hmac.new(
            key.encode(), source.encode(), hashlib.sha256
        ).hexdigest(),
        "redactionTypes": kinds,
    }


def prepare(value, key):
    redaction = redact(value.get("question"), key)
    if not redaction["text"]:
        raise ValueError("生产回放记录缺少 question")
    digest = redaction["sourcePromptHmacSha256"]
    result = {
        "id": "shadow-" + digest[:16],
        "name": "脱敏生产回放 " + digest[:8],
        "question": redaction["text"],
        "capabilityId": value.get("capabilityId")
        if isinstance(value.get("capabilityId"), str) and value["capabilityId"]
        else "unknown",
        "type": value.get("type")
        if isinstance(value.get("type"), str) and value["type"]
        else "generated_project",
        "productionSupported": False,
        "privacy": {
            "schemaVersion": 1,
            "redacted": True,
            "sourcePromptHmacSha256": digest,
            "redactionTypes": redaction["redactionTypes"],
        },
    }
    result.update({field: deepcopy(value[field]) for field in ALLOWED if field in value})
    return result


def attest(value):
    value = value if isinstance(value, dict) else {}
    privacy = value.get("privacy") if isinstance(value.get("privacy"), dict) else {}
    problems = []
    if (
        type(privacy.get("schemaVersion")) is not int
        or privacy["schemaVersion"] != 1
        or privacy.get("redacted") is not True
    ):
        problems.append("生产回放 case 缺少 privacy schemaVersion/redacted 证明")
    if not isinstance(privacy.get("sourcePromptHmacSha256"), str) or not re.fullmatch(
        r"[a-f0-9]{64}", privacy["sourcePromptHmacSha256"]
    ):
        problems.append("生产回放 case 缺少 sourcePromptHmacSha256")
    if not isinstance(privacy.get("redactionTypes"), list) or any(
        not isinstance(item, str) for item in privacy["redactionTypes"]
    ):
        problems.append("生产回放 case redactionTypes 无效")
    if any(
        pattern.search(
            js_string(value.get("question") if value.get("question") is not None else "")
        )
        for _, pattern, _ in PATTERNS
    ):
        problems.append("生产回放 question 仍包含可识别联系方式、证件或账户信息")
    for key in ("userId", "projectId", "requestId", "sessionId", "ipAddress", "email", "phone"):
        if key in value:
            problems.append("生产回放 case 禁止持久化 " + key)
    return {"passed": not problems, "problems": problems}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    key = os.getenv("ALPHADECK_REPLAY_HASH_KEY", "")
    source = Path(args.input).resolve(strict=True)
    content = read_regular(source, 33554432)
    values = (
        [strict_json(line) for line in content.splitlines() if line.strip()]
        if source.suffix == ".jsonl"
        else strict_json(content)
    )
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise ValueError("shadow 输入必须是 JSON 对象数组或 JSONL")
    prepared = {}
    for value in values:
        case = prepare(value, key)
        checked = attest(case)
        if not checked["passed"]:
            raise ValueError("；".join(checked["problems"]))
        prepared[case["privacy"]["sourcePromptHmacSha256"]] = case
    target = Path(args.output).absolute()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_atomic(
        target.parent.resolve(strict=True),
        target.name,
        (json.dumps(list(prepared.values()), ensure_ascii=False, indent=2) + "\n").encode(),
    )
    target.chmod(0o600)
    print(
        json.dumps(
            {"prepared": len(prepared), "inputCount": len(values), "output": str(target)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
