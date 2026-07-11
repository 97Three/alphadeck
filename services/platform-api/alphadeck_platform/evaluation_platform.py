import asyncio
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from .config import ROOT
from .evaluation_cases import CASES_PATH, EvaluationCases
from .evaluation_diagnostics import calibration
from .evaluation_evaluators import definition
from .evaluation_queue import EvaluationQueue, date, virtual_item
from .evaluation_reports import (
    CATALOG,
    analysis,
    coverage,
    fields,
    metadata,
    normalize_run,
    records,
    result,
    text,
)
from .evaluation_scoring import quality_summary, record
from .generation_state import now
from .runtime_store import snake
from .scaffold import controlled_path
from .sessions import iso
from .skill_integrity import read_regular
from .skill_source import strict_json

REPORTS_PATH = "tmp/alphadeck-benchmark-reports"
LOGS_PATH = "tmp/alphadeck-eval-queue/logs"
REPAIRS_PATH = "tmp/alphadeck-eval-repairs/repairs.json"
RUN_COLUMNS = [
    "id",
    "fileName",
    "filePath",
    "reportCreatedAt",
    "mtimeMs",
    "passed",
    "total",
    "passedCount",
    "failedCount",
    "passRate",
    "averageScore",
    "durationMs",
    "metadata",
    "coverage",
    "results",
]
TICKET_COLUMNS = [
    "id",
    "runId",
    "caseId",
    "title",
    "status",
    "severity",
    "model",
    "reportPath",
    "projectId",
    "failures",
    "validationSummaries",
    "suggestedActions",
    "skillVersions",
    "createdAt",
    "updatedAt",
]


def benchmark_args(item):
    arguments = [
        "-B",
        "-m",
        "alphadeck_platform.benchmark_cli",
        "--trigger=eval-backend",
        "--evaluator=" + item["evaluatorId"],
        "--concurrency=" + str(item["concurrency"]),
        "--repeat=" + str(item["repeat"]),
        "--mode=" + item["mode"],
        "--cli=" + item["cli"],
        "--model=" + item["model"],
    ]
    for identity in item["selectedCases"]:
        arguments.extend(("--case", identity))
    if item["limit"]:
        arguments.extend(("--limit", str(item["limit"])))
    if item["keepProjects"]:
        arguments.append("--keep-projects")
    return arguments


def read_json(root, relative, default=None):
    try:
        return strict_json(read_regular(controlled_path(root, relative), 33554432))
    except (OSError, ValueError):
        return default


class EvaluationPlatform:
    def __init__(self, database, root=ROOT):
        self.database, self.root = database, Path(root).resolve(strict=True)
        self.cases, self.queue = EvaluationCases(self.root), EvaluationQueue(database)

    async def persist(self, run):
        values = {**run, "reportCreatedAt": date(run["createdAt"])}
        parameters = tuple(
            Jsonb(values[key]) if key in ("metadata", "coverage", "results") else values[key]
            for key in RUN_COLUMNS
        )
        async with self.database.transaction() as tx:
            await tx.execute(
                sql.SQL(
                    "INSERT INTO eval_runs ({},updated_at) VALUES ({},clock_timestamp()) ON CONFLICT(id) DO UPDATE SET {},updated_at=clock_timestamp()"
                ).format(
                    sql.SQL(",").join(map(lambda key: sql.Identifier(snake(key)), RUN_COLUMNS)),
                    sql.SQL(",").join(sql.Placeholder() for _ in RUN_COLUMNS),
                    sql.SQL(",").join(
                        sql.SQL("{}=excluded.{}").format(
                            sql.Identifier(snake(key)), sql.Identifier(snake(key))
                        )
                        for key in RUN_COLUMNS
                        if key != "id"
                    ),
                ),
                parameters,
            )

    def db_run(self, row):
        covered = coverage(row["coverage"])
        results = [result(item, case_tags=covered["caseTags"]) for item in records(row["results"])]
        meta = metadata(
            {
                "metadata": row["metadata"],
                "schemaVersion": record(row["metadata"]).get("reportSchemaVersion"),
                "createdAt": iso(row["report_created_at"]),
            },
            results,
        )
        return {
            **{
                key: row[snake(key)]
                for key in RUN_COLUMNS
                if key not in ("reportCreatedAt", "metadata", "coverage", "results")
            },
            "createdAt": iso(row["report_created_at"]),
            "metadata": meta,
            "coverage": covered,
            "results": results,
            "qualitySummary": quality_summary(results, meta["selection"]["repeat"]),
            "e2eQuality": meta["e2eQuality"],
        }

    async def runs(self, limit=30):
        database_rows = await self.database.fetch_all(
            "SELECT * FROM eval_runs ORDER BY report_created_at DESC LIMIT %s", (limit,)
        )
        by_id = {row["id"]: self.db_run(row) for row in database_rows}
        directory = controlled_path(self.root, REPORTS_PATH)
        if directory.is_dir():
            cases = self.cases.list()
            for path in directory.iterdir():
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not re.fullmatch(r"report-\d+\.json", path.name)
                ):
                    continue
                raw = read_json(self.root, REPORTS_PATH + "/" + path.name)
                if not isinstance(raw, dict):
                    continue
                run = normalize_run(
                    path, path.stat().st_mtime_ns / 1000000, raw, cases, root=self.root
                )
                await self.persist(run)
                by_id[run["id"]] = run
        return sorted(by_id.values(), key=lambda run: -run["mtimeMs"])[:limit]

    async def run(self, identity):
        if not re.fullmatch(r"report-\d+", identity):
            return None
        path = controlled_path(self.root, REPORTS_PATH + "/" + identity + ".json")
        raw = read_json(self.root, REPORTS_PATH + "/" + identity + ".json")
        if isinstance(raw, dict):
            run = normalize_run(
                path, path.stat().st_mtime_ns / 1000000, raw, self.cases.list(), root=self.root
            )
            await self.persist(run)
            return run
        row = await self.database.fetch_one("SELECT * FROM eval_runs WHERE id=%s", (identity,))
        return self.db_run(row) if row else None

    def ticket(self, value):
        return {
            **fields(
                value,
                textual="id runId caseId title model reportPath",
                nullable="projectId",
                arrays="failures validationSummaries suggestedActions",
            ),
            "status": "resolved" if value.get("status") == "resolved" else "open",
            "severity": "high" if value.get("severity") == "high" else "medium",
            "createdAt": text(value.get("createdAt"), now()),
            "updatedAt": text(value.get("updatedAt"), now()),
            "skillVersions": {
                key: text(item) or None for key, item in record(value.get("skillVersions")).items()
            },
        }

    async def tickets(self):
        legacy = {
            item["id"]: self.ticket(item)
            for item in records(read_json(self.root, REPAIRS_PATH, []))
            if text(item.get("id"))
        }
        rows = await self.database.fetch_all(
            "SELECT * FROM eval_repair_tickets ORDER BY created_at DESC LIMIT 200"
        )
        for row in rows:
            value = {
                key: iso(row[snake(key)]) if key.endswith("At") else row[snake(key)]
                for key in TICKET_COLUMNS
            }
            legacy[row["id"]] = self.ticket(value)
        return sorted(legacy.values(), key=lambda item: item["createdAt"], reverse=True)

    async def create_repairs(self, run):
        if run["passed"]:
            return await self.tickets()
        timestamp = now()
        async with self.database.transaction() as tx:
            await tx.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("eval-repairs:" + run["id"],),
            )
            for item in run["results"]:
                if (
                    item["passed"]
                    or await (
                        await tx.execute(
                            "SELECT id FROM eval_repair_tickets WHERE run_id=%s AND case_id=%s",
                            (run["id"], item["id"]),
                        )
                    ).fetchone()
                ):
                    continue
                actions = []
                for check in item["validationChecks"]:
                    if check["status"] != "failed":
                        continue
                    for keyword, advice in (
                        ("build", "检查生成项目依赖和 Vue/Vite build 输出，优先修复编译错误。"),
                        ("chart", "检查可视化模板是否生成 K 线、成交量或场景化图表容器。"),
                        ("market", "确认 /api/market 同源代理和 Python 行情服务可用。"),
                        ("artifact", "清理外部 CDN、mock 数据、明文密钥和非标准数据绑定。"),
                        (
                            "data",
                            "检查 data_file/final/dashboard-data.json、evidence/sources.json 和 data_quality.json。",
                        ),
                    ):
                        if keyword in check["id"] and advice not in actions:
                            actions.append(advice)
                if record(item.get("eventAudit")).get("errorCount"):
                    actions.append(
                        "检查 .data-agent/events.jsonl 中 error 事件，补齐阶段化执行事件。"
                    )
                value = {
                    "id": "repair-" + str(uuid4()),
                    "runId": run["id"],
                    "caseId": item["id"],
                    "title": item["name"] + " 回归失败",
                    "status": "open",
                    "severity": "high"
                    if any(check["status"] == "failed" for check in item["validationChecks"])
                    else "medium",
                    "model": run["metadata"]["runtime"]["cli"]
                    + " / "
                    + run["metadata"]["runtime"]["model"],
                    "reportPath": run["filePath"],
                    "projectId": item["projectId"],
                    "failures": item["failures"],
                    "validationSummaries": [
                        (check["name"] or check["id"]) + ": " + check["summary"]
                        for check in item["validationChecks"]
                        if check["status"] != "passed"
                    ],
                    "suggestedActions": actions
                    or [
                        "查看失败用例的 failures 和 validation checks，定位生成链路的第一个失败点。"
                    ],
                    "skillVersions": {
                        key: entry["version"]
                        for key, entry in run["metadata"]["skillLockSnapshot"]["skills"].items()
                    },
                    "createdAt": date(timestamp),
                    "updatedAt": date(timestamp),
                }
                await tx.execute(
                    sql.SQL("INSERT INTO eval_repair_tickets ({}) VALUES ({})").format(
                        sql.SQL(",").join(sql.Identifier(snake(key)) for key in TICKET_COLUMNS),
                        sql.SQL(",").join(sql.Placeholder() for _ in TICKET_COLUMNS),
                    ),
                    tuple(
                        Jsonb(value[key])
                        if key
                        in ("failures", "validationSummaries", "suggestedActions", "skillVersions")
                        else value[key]
                        for key in TICKET_COLUMNS
                    ),
                )
        return await self.tickets()

    async def dashboard(self):
        cases, sets = self.cases.list(), self.cases.sets()
        runs, queue, tickets = await asyncio.gather(self.runs(), self.queue.list(), self.tickets())
        async with self.database.transaction() as tx:
            schedule = await self.queue.read_schedule(tx)
        latest = runs[0] if runs else None
        registry = record(read_json(self.root, "benchmarks/alphadeck/datasets.json"))
        snapshot = record(read_json(self.root, "benchmarks/alphadeck/snapshot-manifest.json"))
        mutation_directory = controlled_path(self.root, "tmp/alphadeck-eval-mutations")
        mutations = (
            sorted(
                (
                    path
                    for path in mutation_directory.iterdir()
                    if not path.is_symlink()
                    and path.is_file()
                    and re.fullmatch(r"mutation-report-\d+\.json", path.name)
                ),
                key=lambda path: -path.stat().st_mtime_ns,
            )
            if mutation_directory.is_dir()
            else []
        )
        mutation_path = mutations[0] if mutations else None
        mutation = (
            read_json(self.root, mutation_path.relative_to(self.root).as_posix())
            if mutation_path
            else None
        )
        calibration_path = Path(
            os.getenv("ALPHADECK_EVAL_JUDGE_CALIBRATION_PATH")
            or str(self.root / "benchmarks/alphadeck/judge-calibration.contract.json")
        )
        try:
            calibration_data = record(strict_json(read_regular(calibration_path, 8388608)))
        except (OSError, ValueError):
            calibration_data = {}
        calibrated = (
            calibration(
                calibration_data["samples"],
                independent=calibration_data.get("datasetKind") == "human_blind_calibration",
            )
            if isinstance(calibration_data.get("samples"), list)
            else None
        )
        judge = None
        if calibrated:
            summary = calibrated["summary"]
            judge = {
                "datasetKind": text(calibration_data.get("datasetKind"), "unknown"),
                "productionCalibration": calibration_data.get("datasetKind")
                == "human_blind_calibration",
                "caseCount": summary["caseCount"],
                "agreementRate": summary["verdictAgreementRate"],
                "cohenKappa": summary["cohenKappa"],
                "scoreMeanAbsoluteError": summary["scoreMeanAbsoluteError"],
                "passed": calibrated["passed"],
            }
        production = {case["id"] for case in cases if case["productionSupported"]}
        return {
            "generatedAt": now(),
            "reportsDir": REPORTS_PATH,
            "casesPath": CASES_PATH,
            "runtimeOptions": CATALOG["runtimeOptions"],
            "cases": cases,
            "customEvalSets": sets,
            "runs": runs,
            "queue": queue,
            "repairTickets": tickets,
            "schedule": schedule,
            "latestRun": latest,
            "modelComparison": analysis(runs),
            "skillVersionImpact": analysis(runs, skills=True),
            "assurance": {
                "mutation": {
                    **fields(
                        mutation,
                        textual="createdAt",
                        boolean_fields="baselinePassed",
                        numeric="total killed survived killRate",
                    ),
                    "reportPath": mutation_path.relative_to(self.root).as_posix(),
                }
                if isinstance(mutation, dict)
                else None,
                "datasets": {
                    "publicCaseCount": len(cases),
                    "productionCaseCount": len(production),
                    "productionSnapshotCount": sum(
                        item.get("caseId") in production
                        for item in records(snapshot.get("snapshots"))
                    ),
                    "hiddenConfigured": any(
                        item.get("visibility") == "hidden" and os.getenv(text(item.get("pathEnv")))
                        for item in records(registry.get("datasets"))
                    ),
                    "productionReplayConfigured": any(
                        item.get("visibility") == "production_replay"
                        and os.getenv(text(item.get("pathEnv")))
                        for item in records(registry.get("datasets"))
                    ),
                },
                "judge": judge,
            },
            "summary": {
                "caseCount": len(cases),
                "reportCount": len(runs),
                "capabilityCount": len({case["capabilityId"] for case in cases}),
                **{
                    key: latest[field] if latest else 0
                    for key, field in (
                        ("latestPassRate", "passRate"),
                        ("latestAverageScore", "averageScore"),
                        ("latestPassedCount", "passedCount"),
                        ("latestFailedCount", "failedCount"),
                        ("latestTotal", "total"),
                    )
                },
            },
        }

    async def simulate(self, options):
        item = virtual_item(options)
        all_cases = self.cases.list()
        selected = [
            case
            for case in all_cases
            if not item["selectedCases"] or case["id"] in item["selectedCases"]
        ]
        selected = selected[: item["limit"]] if item["limit"] else selected
        missing = [
            identity
            for identity in item["selectedCases"]
            if not any(case["id"] == identity for case in all_cases)
        ]
        evaluator = definition(item["evaluatorId"])
        steps = []

        def add(identity, name, state, summary, detail=None):
            steps.append(
                {
                    "id": identity,
                    "name": name,
                    "status": state,
                    "summary": summary,
                    "detail": detail,
                }
            )

        add(
            "case-selection",
            "用例选择",
            "passed" if selected and not missing else "failed",
            f"已选择 {len(selected)} 个用例。" if selected else "没有匹配的评测用例。",
            "未找到用例：" + ", ".join(missing) if missing else None,
        )
        add(
            "evaluator",
            "评测器配置",
            "passed",
            f"{item['evaluatorId']}@{evaluator['version']} · 并发 {item['concurrency']} · 重复 {item['repeat']}",
            evaluator["rubricVersion"],
        )
        runtime = next(
            (entry for entry in CATALOG["runtimeOptions"] if entry["cli"] == item["cli"]), None
        )
        known = runtime and any(model["id"] == item["model"] for model in runtime["models"])
        add(
            "runtime",
            "底层执行能力",
            "passed" if known else "warning" if runtime else "failed",
            "已匹配评测器所需的执行能力。" if runtime else "未注册执行能力：" + item["cli"],
            "底层模型不在运行器白名单内，将按传入配置尝试执行。" if runtime and not known else None,
        )
        script = Path(__file__).with_name("benchmark_cli.py")
        add(
            "benchmark-script",
            "Benchmark 脚本",
            "passed" if script.is_file() else "failed",
            "benchmark 入口脚本存在。" if script.is_file() else "benchmark 入口脚本缺失。",
            "alphadeck_platform/benchmark_cli.py",
        )
        try:
            await self.database.fetch_one("SELECT id,lease_token FROM eval_queue_items LIMIT 1")
            queue_ready = True
        except Exception:
            queue_ready = False
        add(
            "queue-database",
            "持久化队列",
            "passed" if queue_ready else "failed",
            "PostgreSQL 评测队列及租约字段可用。"
            if queue_ready
            else "评测队列不可用，请检查数据库连接和版本迁移。",
        )
        writable = True
        for relative in (LOGS_PATH, REPORTS_PATH, str(Path(REPAIRS_PATH).parent)):
            try:
                controlled_path(self.root, relative).mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError):
                writable = False
        add(
            "storage",
            "本地存储",
            "passed" if writable else "failed",
            "日志、报告和修复单目录可写。" if writable else "部分评测目录不可写。",
            " · ".join(
                (
                    "tmp/alphadeck-eval-queue",
                    LOGS_PATH,
                    REPORTS_PATH,
                    str(Path(REPAIRS_PATH).parent),
                )
            ),
        )
        runs, tickets = await self.runs(3), await self.tickets()
        add(
            "report-parser",
            "报告解析",
            "passed" if runs else "warning",
            f"已解析 {len(runs)} 份最近报告。"
            if runs
            else "暂无历史报告，首次运行后才会生成运行记录。",
            runs[0]["filePath"] if runs else None,
        )
        add(
            "repair-sink",
            "修复单沉淀",
            "passed",
            f"修复单存储可读，当前 {len(tickets)} 条。",
            REPAIRS_PATH,
        )
        command = [sys.executable, *benchmark_args(item)]
        add("command", "执行命令", "passed", "已生成 benchmark 执行命令。", " ".join(command))
        return {
            "generatedAt": now(),
            "ready": all(step["status"] != "failed" for step in steps),
            "runtime": {key: item[key] for key in ("cli", "model", "reasoningEffort", "mode")},
            "evaluator": {"id": item["evaluatorId"], "concurrency": item["concurrency"]},
            "selection": {
                key: item[key]
                for key in ("selectedCases", "limit", "keepProjects", "concurrency", "repeat")
            }
            | {"caseCount": len(selected)},
            "selectedCaseIds": [case["id"] for case in selected],
            "command": command,
            "steps": steps,
            "warnings": [step["summary"] for step in steps if step["status"] == "warning"],
        }
