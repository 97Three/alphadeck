import json
import re

from .file_tools import truncate
from .messages import js_length, js_slice
from .provenance import js_json


def json_path(parent, key):
    if type(key) is int:
        return f"{parent}[{key}]"
    return (
        parent + "." + key
        if re.fullmatch(r"[A-Za-z_$][\w$]*", key, re.ASCII)
        else parent + "[" + js_json(key) + "]"
    )


def compact_string(value, limit):
    if js_length(value) <= limit:
        return value
    head = max(1, limit // 3)
    tail = max(0, limit - head)
    return js_slice(value, 0, head) + (js_slice(value, -tail) if tail else "")


def project_value(value, array_items, string_chars, path, state, depth=0):
    if depth > 64:
        raise ValueError("Market response nesting exceeds projection budget")

    def omitted(kind, original, retained):
        state["count"] += 1
        if len(state["omissions"]) < 24:
            state["omissions"].append(
                {"path": path, "kind": kind, "originalSize": original, "retainedSize": retained}
            )

    if isinstance(value, str):
        if js_length(value) <= string_chars:
            return value
        result = compact_string(value, string_chars)
        omitted("string", js_length(value), js_length(result))
        return result
    if isinstance(value, list):
        if len(value) <= array_items:
            indexes = list(range(len(value)))
        elif array_items <= 1:
            indexes = [len(value) - 1] if array_items == 1 else []
        else:
            head = max(1, array_items // 3)
            indexes = [*range(head), *range(len(value) - array_items + head, len(value))]
        if len(indexes) != len(value):
            omitted("array", len(value), len(indexes))
        return [
            project_value(
                value[index], array_items, string_chars, json_path(path, index), state, depth + 1
            )
            for index in indexes
        ]
    if isinstance(value, dict):
        return {
            key: project_value(
                item, array_items, string_chars, json_path(path, key), state, depth + 1
            )
            for key, item in value.items()
        }
    return value


def bounded_preview(value, limit, metadata):
    low, high, fitted = 0, min(js_length(value), limit), ""
    while low <= high:
        size = (low + high) // 2
        candidate = js_json({"$piAgent": metadata, "preview": compact_string(value, size)})
        if js_length(candidate) <= limit:
            fitted, low = candidate, size + 1
        else:
            high = size - 1
    return (
        fitted
        or truncate(
            js_json(
                {"$piAgent": {"kind": "quant_api_result_window", "version": 1, "truncated": True}}
            ),
            limit,
        )["text"]
    )


def compact_quant_output(value, *, limit, response_bytes, response_byte_limit_reached, path):
    length = js_length(value)
    if not response_byte_limit_reached and length <= limit:
        return {"text": value, "truncated": False}
    metadata = {
        "kind": "quant_api_result_window",
        "version": 1,
        "truncated": True,
        "sourcePath": path,
        "responseBytesRead": response_bytes,
        "responseByteLimitReached": response_byte_limit_reached,
    }
    if not response_byte_limit_reached:
        try:
            parsed = json.loads(value)
            for items, chars in (
                (128, 4096),
                (64, 2048),
                (32, 1024),
                (16, 512),
                (8, 256),
                (4, 128),
                (2, 96),
                (1, 64),
            ):
                state = {"count": 0, "omissions": []}
                data = project_value(parsed, items, chars, "$", state)
                candidate = js_json(
                    {
                        "$piAgent": {
                            **metadata,
                            "strategy": "head_and_recent_tail",
                            "originalCharacters": length,
                            "omissionCount": state["count"],
                            "omissions": state["omissions"],
                            "omissionDetailsTruncated": state["count"] > len(state["omissions"]),
                        },
                        "data": data,
                    }
                )
                if js_length(candidate) <= limit:
                    return {"text": candidate, "truncated": True, "strategy": "json_window"}
        except (ValueError, RecursionError):
            pass
    strategy = "response_byte_limit" if response_byte_limit_reached else "bounded_preview"
    return {
        "text": bounded_preview(
            value,
            limit,
            {
                **metadata,
                "strategy": strategy,
                "originalCharactersRead": length,
                "retryHint": "Retry with a narrower date range, smaller limit, or smaller page_size."
                if response_byte_limit_reached
                else "The response was not valid compactable JSON; use a narrower query if more detail is required.",
            },
        ),
        "truncated": True,
        "strategy": strategy,
    }
