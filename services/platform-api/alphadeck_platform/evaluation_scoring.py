import math

from .finance_values import left_sum

DIMENSIONS = ("contract", "grounding", "task", "visual", "reliability", "efficiency", "safety")


def record(value):
    return value if isinstance(value, dict) else {}


def number(value, default=0):
    try:
        return value if type(value) in {int, float} and math.isfinite(value) else default
    except OverflowError:
        return default


def rounded(value, digits=2):
    value = number(value)
    factor = 10 ** max(0, math.floor(digits))
    return math.floor(value * factor + 0.5) / factor


def score(value, default=0):
    return min(100, max(0, math.floor(number(value, default) + 0.5)))


def percentage(numerator, denominator):
    return math.floor(numerator / denominator * 100 + 0.5) if denominator > 0 else 0


def percentile(values, quantile):
    return (
        sorted(values)[min(len(values) - 1, max(0, math.ceil(len(values) * quantile) - 1))]
        if values
        else 0
    )


def wilson_interval(passed, total, z=1.959963984540054):
    if not number(total) or total <= 0:
        return {"lower": 0, "upper": 0}
    sample = max(1, math.floor(total))
    successes = min(sample, max(0, math.floor(passed)))
    proportion, squared = successes / sample, z**2
    denominator = 1 + squared / sample
    centre = proportion + squared / (2 * sample)
    margin = z * math.sqrt((proportion * (1 - proportion) + squared / (4 * sample)) / sample)
    return {
        "lower": rounded(max(0, (centre - margin) / denominator * 100)),
        "upper": rounded(min(100, (centre + margin) / denominator * 100)),
    }


def deviation(values):
    values = [item for item in values if number(item, None) is not None]
    if len(values) <= 1:
        return 0
    average = left_sum(values) / len(values)
    return rounded(math.sqrt(left_sum((item - average) ** 2 for item in values) / len(values)))


def result_score(value):
    result = record(value)
    evaluation = record(result.get("evaluation"))
    if number(evaluation.get("score"), None) is not None:
        return score(evaluation["score"])
    if number(result.get("score"), None) is not None:
        return score(result["score"])
    failures = len(result["failures"]) if isinstance(result.get("failures"), list) else 0
    return 100 if result.get("passed") is True else max(0, 60 - failures * 12)


def weighted_score(dimensions):
    total = left_sum(item["weight"] for item in dimensions)
    return (
        score(left_sum(score(item["score"]) * item["weight"] for item in dimensions) / total)
        if total > 0
        else 0
    )


def quality_summary(values, repeat_count=1):
    results = [record(item) for item in values]
    count = len(results)
    first = sum(item.get("firstPassPassed") is True for item in results)
    final = sum(item.get("finalPassed") is True or item.get("passed") is True for item in results)
    repaired = sum(number(item.get("repairAttempts")) > 0 for item in results)
    durations = [max(0, number(item.get("durationMs"))) for item in results]
    totals, deviations, flaky = {key: [] for key in DIMENSIONS}, [], []
    attempts_count, passed_count = 0, 0
    for result in results:
        evaluation = record(result.get("evaluation"))
        dimensions = evaluation.get("dimensions")
        for item in dimensions if isinstance(dimensions, list) else []:
            item = record(item)
            if isinstance(item.get("id"), str) and item["id"] in totals:
                totals[item["id"]].append(score(item.get("score")))
        attempts = record(result.get("stability")).get("attempts")
        attempts = (
            [record(item) for item in attempts]
            if isinstance(attempts, list) and attempts
            else [{"passed": result.get("passed") is True}]
        )
        passed = sum(item.get("passed") is True for item in attempts)
        case = result["id"] if isinstance(result.get("id"), str) else "unknown"
        attempts_count += len(attempts)
        passed_count += passed
        deviations.append(
            {"id": case, "value": deviation([result_score(item) for item in attempts])}
        )
        if 0 < passed < len(attempts):
            flaky.append(case)

    def average(items):
        return math.floor(left_sum(items) / len(items) + 0.5) if items else 0

    maximum = {"id": None, "value": 0}
    for item in deviations:
        if item["value"] > maximum["value"]:
            maximum = item
    return {
        "caseCount": count,
        "firstPassCount": first,
        "finalPassCount": final,
        "firstPassRate": percentage(first, count),
        "finalPassRate": percentage(final, count),
        "repairedCaseCount": repaired,
        "repairRate": percentage(repaired, count),
        "averageScore": average([result_score(item) for item in results]),
        "durationMs": {
            "total": left_sum(durations),
            "average": average(durations),
            "p50": percentile(durations, 0.5),
            "p95": percentile(durations, 0.95),
        },
        "dimensions": {key: average(items) for key, items in totals.items()},
        "stability": {
            "repeatCount": max(1, math.floor(repeat_count)),
            "attemptCount": attempts_count,
            "passedAttemptCount": passed_count,
            "passRate": percentage(passed_count, attempts_count),
            "flakyCaseIds": flaky,
            "confidence95": wilson_interval(passed_count, attempts_count),
            "scoreStdDev": {
                "average": rounded(left_sum(item["value"] for item in deviations) / len(deviations))
                if deviations
                else 0,
                "max": maximum,
            },
        },
    }


def deterministic_gate(value):
    result, failures = record(value), []
    if result.get("passed") is not True:
        failures.append("runner_not_passed")
    if isinstance(result.get("failures"), list) and result["failures"]:
        failures.append("runner_has_failures")
    for key in ("validation", "artifacts"):
        stage = record(result.get(key))
        if stage.get("status") == "failed" or stage.get("passed") is False:
            failures.append(key + "_failed")
        checks = stage.get("checks") if isinstance(stage.get("checks"), list) else []
        if any(
            record(item).get("status") == "failed" or record(item).get("passed") is False
            for item in checks
        ):
            failures.append(key + "_check_failed")
    oracle = record(record(result.get("artifacts")).get("oracle"))
    if (
        oracle.get("passed") is False
        or isinstance(oracle.get("checks"), list)
        and any(
            record(item).get("passed") is not True and record(item).get("severity") != "warning"
            for item in oracle["checks"]
        )
    ):
        failures.append("oracle_failed")
    if (
        result.get("visualCheck") is not None
        and record(result["visualCheck"]).get("passed") is not True
    ):
        failures.append("visual_failed")
    for label, source, field in (
        ("event_error", record(result.get("eventAudit")), "errorCount"),
        (
            "unexpected_tool_failure",
            record(record(result.get("agentExecution")).get("tools")),
            "unexpectedFailureCount",
        ),
    ):
        if field not in source:
            continue
        count = source[field]
        if type(count) is not int or not 0 <= count <= 9007199254740991:
            failures.append(label + "_count_invalid")
        elif count:
            failures.append(label)
    return {"passed": not failures, "failures": failures}
