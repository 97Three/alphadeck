import hashlib
import json
import math
import re

from .http_input import reject_constant
from .messages import js_length
from .provenance import js_json
from .scaffold import controlled_path
from .skill_integrity import read_regular

ARTIFACTS = {
    "finalData": "data_file/final/dashboard-data.json",
    "sources": "evidence/sources.json",
    "quality": "evidence/data_quality.json",
    "runPlan": ".data-agent/finance-run-plan.json",
}


def project(value, limit, depth=0):
    if depth > 24:
        return {"reviewOmitted": True}
    if isinstance(value, str) and js_length(value) > 2048:
        return {"reviewOmitted": True, "characters": js_length(value)}
    if isinstance(value, list):
        count = math.ceil(limit / 3)
        retained = value if len(value) <= limit else [*value[:count], *value[-(limit - count) :]]
        return [project(item, limit, depth + 1) for item in retained]
    if isinstance(value, dict):
        return {key: project(item, limit, depth + 1) for key, item in list(value.items())[:128]}
    return value


def load_evidence(root):
    result = {}
    for identity, relative in ARTIFACTS.items():
        try:
            content = read_regular(controlled_path(root, relative), 2 * 1024 * 1024)
            original = json.loads(content.decode("utf-8"), parse_constant=reject_constant)
        except (OSError, ValueError) as cause:
            raise ValueError(
                "Review evidence is missing, unsafe or invalid: " + identity
            ) from cause
        if not isinstance(original, dict):
            raise ValueError("Review evidence must be a JSON object: " + identity)
        value, raw = original, js_json(original)
        if js_length(raw) > 24000:
            for limit in (64, 16, 4, 2):
                value = project(original, limit)
                if js_length(js_json(value)) <= 24000:
                    break
        serialized = js_json(value)
        if js_length(serialized) > 24000:
            raise ValueError("Review evidence cannot fit its context budget: " + identity)
        result[identity] = {
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "path": relative,
            "value": value,
            "truncated": serialized != raw,
        }
    return result


def resolves(reference, evidence):
    if not isinstance(reference, str) or js_length(reference) > 300:
        return False
    match = re.fullmatch(r"(finalData|sources|quality|runPlan)#(/[^#]+)", reference)
    if not match:
        return False
    value = (evidence.get(match[1]) or {}).get("value")
    for part in match[2][1:].split("/"):
        if re.search(r"~(?![01])", part):
            return False
        key = part.replace("~1", "/").replace("~0", "~")
        if key in {"__proto__", "prototype", "constructor", "reviewOmitted"}:
            return False
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif (
            isinstance(value, list)
            and re.fullmatch(r"0|[1-9][0-9]*", key)
            and int(key) < len(value)
        ):
            value = value[int(key)]
        elif isinstance(value, list) and key == "length":
            value = len(value)
        else:
            return False
    return (
        value is not None
        and value != ""
        and not (isinstance(value, dict) and "reviewOmitted" in value)
    )
