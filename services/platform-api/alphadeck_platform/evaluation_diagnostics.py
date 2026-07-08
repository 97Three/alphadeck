import re

from .evaluation_queue import strings
from .evaluation_reports import records, text
from .evaluation_scoring import deterministic_gate, number, record, rounded
from .finance_values import js_string, left_sum, numeric


def js_number(value):
    if value is None or value == "" or isinstance(value, str) and not value.strip():
        return 0
    if type(value) is bool:
        return int(value)
    if isinstance(value, list):
        return js_number(js_string(value))
    result = numeric(value)
    return result if result is not None else float("nan")


def equal(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            equal(item, right[key]) for key, item in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    return type(left) is type(right) and left == right


def trace_diagnostics(value, mode):
    value = record(value)
    gate = deterministic_gate({**value, "passed": True, "failures": []})
    validation = record(value.get("validation"))
    checks = records(
        validation.get("checks")
        if isinstance(validation.get("checks"), list)
        else value.get("validationChecks")
    )
    failures = strings(value.get("failures"))
    oracle = record(record(value.get("artifacts")).get("oracle"))
    audit, execution = record(value.get("eventAudit")), record(value.get("agentExecution"))
    observed = strings(audit.get("stages"))

    def stage(ids, default="unknown"):
        selected = [
            item
            for item in checks
            if js_string(item.get("id") if item.get("id") is not None else "") in ids
        ]
        signals = [
            js_string(item.get("id", "undefined"))
            + ":"
            + js_string(item.get("status") if item.get("status") is not None else "unknown")
            for item in selected
        ]
        states = [item.get("status") for item in selected]
        return {
            "status": "failed"
            if "failed" in states
            else "warning"
            if "warning" in states or "unknown" in states
            else "passed"
            if selected
            else default,
            "signals": signals,
        }

    intent = stage(("intent_clarification", "clarification_continuation"))
    intent_failures = [item for item in failures if re.search("澄清|意图|symbol|标的", item)]
    intent["status"] = (
        "failed"
        if intent_failures
        else "passed"
        if intent["status"] == "unknown"
        else intent["status"]
    )
    intent["signals"] += intent_failures[:5]
    planning = ["event:" + item for item in observed if re.search("plan|规划|mission", item, re.I)]
    planning_status = (
        "failed"
        if any(re.search("run_plan|规划|plan", item, re.I) for item in failures)
        else "passed"
        if planning or value.get("prefetch") is not None
        else "unknown"
    )
    data = stage(("final_data_file", "evidence_files", "market_proxy"))
    data["status"] = (
        "failed"
        if "oracle_failed" in gate["failures"]
        else "warning"
        if oracle.get("warning") is True
        else data["status"]
    )
    if oracle.get("passed") is False:
        data["signals"].append("oracle:failed")
    artifact = stage(
        (
            "artifact_policy",
            "vue_build",
            "preview_http_200",
            "artifact_contracts",
            "dashboard_data_binding",
            "chart_presence",
        )
    )
    visual = stage(("visual_presentation",))
    if value.get("visualCheck") is not None:
        visual["status"] = (
            "passed" if record(value["visualCheck"]).get("passed") is True else "failed"
        )
    tool_failures, errors, warnings = [
        js_number(raw if raw is not None else 0)
        for raw in (
            record(execution.get("tools")).get("unexpectedFailureCount"),
            audit.get("errorCount"),
            audit.get("warningCount"),
        )
    ]
    runtime = (
        "failed"
        if any(item.endswith("_count_invalid") for item in gate["failures"])
        or tool_failures > 0
        or errors > 0
        else "warning"
        if warnings > 0
        else "passed"
    )
    acceptance = (
        "passed"
        if mode == "contract"
        or execution.get("executed") is True
        and execution.get("acceptedReceiptId")
        and execution.get("missionStatus") in ("accepted", "completed")
        else "failed"
    )
    stages = [
        {"id": "intent", "label": "意图与标的", **intent},
        {"id": "planning", "label": "规划", "status": planning_status, "signals": planning},
        {"id": "data", "label": "数据与事实", **data},
        {"id": "artifact", "label": "产物与构建", **artifact},
        {"id": "visual", "label": "视觉交付", **visual},
        {
            "id": "runtime",
            "label": "运行可靠性",
            "status": runtime,
            "signals": [
                "eventErrors:" + js_string(errors),
                "eventWarnings:" + js_string(warnings),
                "unexpectedToolFailures:" + js_string(tool_failures),
            ],
        },
        {
            "id": "acceptance",
            "label": "Mission 验收",
            "status": acceptance,
            "signals": [
                "mission:"
                + js_string(
                    execution.get("missionStatus")
                    if execution.get("missionStatus") is not None
                    else "missing"
                )
            ]
            if mode == "e2e"
            else ["contract:no-live-mission"],
        },
    ]
    return {
        "schemaVersion": 1,
        "primaryFailureStage": next(
            (item["id"] for item in stages if item["status"] == "failed"), None
        ),
        "stages": stages,
        "observedEventStages": observed,
    }


def calibration(samples, *, min_agreement=80, min_kappa=0.6, max_error=15, independent=False):
    count = len(samples)
    agreements = sum(item["human"]["verdict"] == item["judge"]["verdict"] for item in samples)
    observed = agreements / count if count else 0
    expected = (
        left_sum(
            sum(item["human"]["verdict"] == verdict for item in samples)
            / count
            * sum(item["judge"]["verdict"] == verdict for item in samples)
            / count
            for verdict in ("passed", "warning", "failed")
        )
        if count
        else 0
    )
    kappa = (
        (1 if observed == 1 else 0)
        if expected == 1
        else (observed - expected) / (1 - expected)
        if count
        else 0
    )
    errors = [
        {
            "caseId": item["caseId"],
            "value": abs(
                min(100, max(0, number(item["human"]["score"])))
                - min(100, max(0, number(item["judge"]["score"])))
            ),
        }
        for item in samples
    ]
    maximum = {"caseId": None, "value": 0}
    for item in errors:
        if item["value"] > maximum["value"]:
            maximum = item
    summary = {
        "caseCount": count,
        "agreementCount": agreements,
        "verdictAgreementRate": rounded(observed * 100),
        "cohenKappa": rounded(kappa, 3),
        "scoreMeanAbsoluteError": rounded(left_sum(item["value"] for item in errors) / count)
        if count
        else 0,
        "worstScoreError": maximum,
        "independentSampleCount": sum(
            bool(item["judge"].get("independentFromGenerator")) for item in samples
        ),
        "disagreements": [
            item["caseId"]
            for item in samples
            if item["human"]["verdict"] != item["judge"]["verdict"]
        ],
    }
    problems = []
    if not count:
        problems.append("Judge 校准集不能为空")
    for condition, message in (
        (
            summary["verdictAgreementRate"] < min_agreement,
            f"Judge verdict 一致率 {js_string(summary['verdictAgreementRate'])}% 低于 {js_string(min_agreement)}%",
        ),
        (
            summary["cohenKappa"] < min_kappa,
            f"Judge Cohen kappa {js_string(summary['cohenKappa'])} 低于 {js_string(min_kappa)}",
        ),
        (
            summary["scoreMeanAbsoluteError"] > max_error,
            f"Judge 分数 MAE {js_string(summary['scoreMeanAbsoluteError'])} 高于 {js_string(max_error)}",
        ),
        (
            independent and summary["independentSampleCount"] != count,
            f"Judge 独立样本仅 {summary['independentSampleCount']}/{count}",
        ),
    ):
        if condition:
            problems.append(message)
    return {"passed": not problems, "problems": problems, "summary": summary}


async def inspect_backtest(backtest, raw_files, read_artifact):
    if not isinstance(backtest, dict):
        return ["缺少 backtest 结果。"]
    if "experiment" in backtest:
        return ["final 回测不应重复包含实验输入。"]
    if "experiment_ref" not in backtest:
        return (
            []
            if any(item.endswith("/backtest-ma-crossover.json") for item in raw_files)
            else ["缺少历史回测 raw 产物。"]
        )
    ref, hashes = backtest.get("experiment_ref"), ("experiment_id", "data_sha256", "result_sha256")
    if not isinstance(ref, dict) or any(
        not re.fullmatch(r"sha256:[a-f0-9]{64}", text(ref.get(key))) for key in hashes
    ):
        return ["回测实验引用缺少有效的实验、输入或结果摘要。"]
    if ref.keys() - {"artifact_path", *hashes}:
        return ["回测实验引用应只包含归档路径和摘要。"]
    path = ref.get("artifact_path")
    filename = "backtest-ma-crossover-" + ref["experiment_id"][7:] + ".json"
    if (
        not isinstance(path, str)
        or not re.fullmatch(r"data_file/raw/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/[^/]+", path)
        or any(part in (".", "..") for part in path.split("/"))
        or not path.endswith("/" + filename)
        or path not in raw_files
    ):
        return ["回测实验引用必须指向本次预取归档中与实验 ID 对应的安全路径。"]
    raw = record(await read_artifact(path))
    experiment = record(raw.get("experiment"))
    if (
        not raw
        or not experiment
        or type(experiment.get("schema_version")) is not int
        or experiment["schema_version"] != 1
    ):
        return ["回测归档缺少受支持的实验记录。"]
    failures = []
    if any(experiment.get(key) != ref[key] for key in hashes):
        failures.append("回测归档与 final 引用的摘要不一致。")
    bars = record(experiment.get("data")).get("bars")
    if not isinstance(bars, list) or not 1 <= len(bars) <= 1500:
        failures.append("回测归档缺少有界的行情输入。")
    if not equal(
        {key: item for key, item in raw.items() if key != "experiment"},
        {key: item for key, item in backtest.items() if key != "experiment_ref"},
    ):
        failures.append("final 回测结果与归档结果不一致。")
    return failures
