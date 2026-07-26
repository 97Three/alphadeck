import math
import re
from copy import deepcopy

from .messages import js_length

ACTIVE = {"pending", "running", "reconciling", "waiting"}
TERMINAL = {"candidate_complete", "completed", "failed", "cancelled", "timed_out", "interrupted"}
BOUNDARIES = {
    "run_started",
    "model_turn_completed",
    "tools_completed",
    "waiting_for_external_input",
}
EFFECTS = {"pure", "read", "workspace_write", "external_write"}
IDEMPOTENCY = {"intrinsic", "operation_key", "reconcile_required"}
UNRESOLVED = {"prepared", "commit_authorized", "uncertain"}
FORBIDDEN = {
    "reasoning",
    "reasoningcontent",
    "chainofthought",
    "internalthoughts",
    "systemprompt",
    "messages",
    "rawproviderresponse",
    "rawrequest",
    "rawresponse",
    "rawcause",
    "stack",
    "authorization",
    "cookie",
    "setcookie",
    "apikey",
    "secret",
    "accesstoken",
    "refreshtoken",
}


class RuntimeStoreError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def integer(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= 9007199254740991:
        raise RuntimeStoreError("INVALID_STATE", "Expected a non-negative safe integer.")


def identifier(value, maximum=256):
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= maximum
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+/@=-]*", value)
    ):
        raise RuntimeStoreError("INVALID_STATE", "Expected a bounded ASCII identifier.")


def public_json(value):
    nodes = 0
    ancestors = set()

    def visit(candidate, depth):
        nonlocal nodes
        nodes += 1
        if nodes > 20000 or depth > 24:
            raise RuntimeStoreError(
                "INVALID_STATE", "Public runtime JSON exceeds its structural budget."
            )
        if candidate is None or type(candidate) is bool:
            return
        if type(candidate) in {int, float}:
            if not math.isfinite(candidate):
                raise RuntimeStoreError(
                    "INVALID_STATE", "Public runtime JSON contains a non-finite number."
                )
            return
        if isinstance(candidate, str):
            if len(candidate.encode("utf-8", errors="replace")) > 524288:
                raise RuntimeStoreError(
                    "INVALID_STATE", "Public runtime JSON contains an oversized string."
                )
            return
        if type(candidate) not in {dict, list} or id(candidate) in ancestors:
            raise RuntimeStoreError(
                "INVALID_STATE", "Public runtime JSON contains a cycle or non-JSON value."
            )
        ancestors.add(id(candidate))
        if isinstance(candidate, dict):
            for key, child in candidate.items():
                if (
                    not isinstance(key, str)
                    or not 0 < js_length(key) <= 160
                    or key in {"__proto__", "prototype", "constructor"}
                    or re.sub(r"[_\-.\s]", "", key).lower() in FORBIDDEN
                ):
                    raise RuntimeStoreError(
                        "INVALID_STATE", "Public runtime JSON contains a forbidden key."
                    )
                visit(child, depth + 1)
        else:
            for child in candidate:
                visit(child, depth + 1)
        ancestors.remove(id(candidate))

    if type(value) is not dict:
        raise RuntimeStoreError("INVALID_STATE", "Public runtime JSON must be an object.")
    visit(value, 0)
    return deepcopy(value)


def usage(value):
    keys = (
        "inputTokens",
        "outputTokens",
        "totalTokens",
        "cachedInputTokens",
        "cacheMissInputTokens",
        "reasoningTokens",
    )
    result = {key: value.get(key, 0) for key in keys}
    for count in result.values():
        integer(count)
    if result["totalTokens"] < max(result["inputTokens"], result["outputTokens"]):
        raise RuntimeStoreError(
            "INVALID_STATE", "Total usage cannot be smaller than input or output usage."
        )
    return result
