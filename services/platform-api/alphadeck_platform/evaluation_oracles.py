"""Deterministic assertions over financial data and independently observed artifacts."""

import re

from .finance_values import js_string
from .messages import js_length, js_slice
from .provenance import js_json

MISSING = object()
OPERATORS = {
    "exists",
    "not_exists",
    "equals",
    "not_equals",
    "gte",
    "lte",
    "between",
    "contains",
    "not_contains",
    "matches",
    "not_matches",
    "length_gte",
    "length_lte",
}
TARGETS = {"finalData", "sources", "quality", "page"}


def at_path(root, path=""):
    current = root
    for segment in filter(None, path.split(".")):
        if isinstance(current, list) and re.fullmatch(r"[0-9]+", segment):
            index = int(segment)
            current = current[index] if index < len(current) else MISSING
        elif isinstance(current, dict):
            current = current.get(segment, MISSING)
        else:
            return MISSING
    return current


def pattern(value):
    if not isinstance(value, str) or js_length(value) > 500:
        return None
    try:
        return re.compile(value, re.I)
    except re.error:
        return None


def encoded(value):
    return None if value is MISSING else js_json(value)


def numeric(value):
    return type(value) in {int, float}


def matches(assertion, actual):
    operator, expected = assertion["operator"], assertion.get("value", MISSING)
    if operator == "exists":
        return actual is not MISSING and actual is not None
    if operator == "not_exists":
        return actual is MISSING or actual is None
    if operator == "equals":
        return (
            abs(actual - expected) <= max(0, assertion.get("tolerance", 0))
            if numeric(actual) and numeric(expected)
            else encoded(actual) == encoded(expected)
        )
    if operator == "not_equals":
        return encoded(actual) != encoded(expected)
    if operator in {"gte", "lte"}:
        return (
            numeric(actual)
            and numeric(expected)
            and (actual >= expected if operator == "gte" else actual <= expected)
        )
    if operator == "between":
        return (
            numeric(actual)
            and numeric(assertion.get("min"))
            and numeric(assertion.get("max"))
            and assertion["min"] <= actual <= assertion["max"]
        )
    if operator in {"contains", "not_contains"}:
        text = "" if expected is None or expected is MISSING else js_string(expected)
        return isinstance(actual, str) and (
            bool(text) and text in actual if operator == "contains" else text not in actual
        )
    if operator in {"matches", "not_matches"}:
        expression = pattern(expected)
        return (
            isinstance(actual, str)
            and expression is not None
            and (
                bool(expression.search(actual))
                if operator == "matches"
                else not expression.search(actual)
            )
        )
    if operator in {"length_gte", "length_lte"}:
        length = (
            js_length(actual)
            if isinstance(actual, str)
            else len(actual)
            if isinstance(actual, (list, dict))
            else None
        )
        return (
            length is not None
            and numeric(expected)
            and (length >= expected if operator == "length_gte" else length <= expected)
        )
    return False


def evidence(value):
    if isinstance(value, list):
        return {"type": "array", "length": len(value)}
    if isinstance(value, str):
        return value if js_length(value) <= 500 else js_slice(value, 0, 500) + "...[truncated]"
    if isinstance(value, dict):
        return {"type": "object", "keys": list(value)[:50]}
    return value


def evaluate(assertions, targets):
    checks = []
    for assertion in assertions or []:
        actual = at_path(targets.get(assertion["target"], MISSING), assertion.get("path", ""))
        passed = bool(matches(assertion, actual))
        checks.append(
            {
                "id": assertion["id"],
                "target": assertion["target"],
                "operator": assertion["operator"],
                "passed": passed,
                "severity": assertion.get("severity", "error"),
                "summary": assertion.get("message")
                if assertion.get("message") is not None
                else f"{assertion['target']}.{assertion.get('path', '')} "
                + (
                    assertion["operator"] + " 通过" if passed else "未满足 " + assertion["operator"]
                ),
                **({"actual": evidence(actual)} if actual is not MISSING else {}),
            }
        )
    failures = [
        f"oracle:{item['id']} {item['summary']}"
        for item in checks
        if not item["passed"] and item["severity"] == "error"
    ]
    warnings = [
        f"oracle:{item['id']} {item['summary']}"
        for item in checks
        if not item["passed"] and item["severity"] == "warning"
    ]
    return {
        "passed": not failures,
        "warning": bool(warnings),
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
    }
