import asyncio
import copy
import time
from uuid import uuid4

from .finance_catalog import FinanceCatalog
from .finance_values import numeric
from .generation_state import now
from .projects import serialize_project
from .strategy_mappers import CATALOG, array, mapped, record
from .strategy_scans import StrategyScans


def expand_grid(scan):
    values = [{}]
    for item in scan["grid"]:
        values = [
            {**previous, item["key"]: value}
            for previous in (values or [{}])
            for value in item["values"]
        ]
    return values if scan["grid"] else []


def best_result(results):
    successes = [item for item in results if item["status"] == "success"]
    return (
        min(
            successes,
            key=lambda item: (
                -(
                    item["metrics"].get("totalReturnPct")
                    if item["metrics"].get("totalReturnPct") is not None
                    else -float("inf")
                ),
                item["metrics"].get("maxDrawdownPct")
                if item["metrics"].get("maxDrawdownPct") is not None
                else float("inf"),
            ),
        )
        if successes
        else None
    )


class Strategies:
    def __init__(self, projects, market, *, scans_root=None):
        self.projects, self.market = projects, market
        self.scans = StrategyScans(projects.database, scans_root)

    @staticmethod
    def template(identity):
        value = next((item for item in CATALOG["templates"] if item["id"] == identity), None)
        if value is None:
            raise ValueError("Unknown strategy template: " + identity)
        return copy.deepcopy(value)

    def scan_definition(self, template_id, scan_id):
        template = self.template(template_id)
        scan = next((item for item in template["parameterScans"] if item["id"] == scan_id), None)
        if scan is None:
            raise ValueError("Unknown parameter scan: " + scan_id)
        return template, scan

    def prompt(self, template_id, symbol=None):
        template = next(
            (item for item in CATALOG["templates"] if item["id"] == template_id),
            CATALOG["templates"][0],
        )
        target = (
            symbol.strip()
            if isinstance(symbol, str) and symbol.strip()
            else template["defaultSymbols"][0]
        )
        capability = next(
            item
            for item in FinanceCatalog().data["capabilities"]
            if item["id"] == template["capabilityId"]
        )
        return {
            "name": template["name"] + " · " + target,
            "prompt": template["promptSeed"]
            + f"\n\n策略模板：{template['name']}\n目标标的：{target}\n能力模块：{capability['name']}\n必须展示参数、数据来源、风险限制和验证结论边界。",
            "capabilityId": capability["id"],
        }

    async def scan(self, template_id, scan_id, symbol=None):
        template, scan = self.scan_definition(template_id, scan_id)
        started = now()
        target = (
            symbol.strip()
            if isinstance(symbol, str) and symbol.strip()
            else (template["defaultSymbols"] or ["510300"])[0]
        )
        combinations, results = expand_grid(scan)[:64], []
        for index, parameters in enumerate(combinations):
            item = {
                "id": scan["id"] + "-" + str(index + 1),
                "parameters": parameters,
                "status": "skipped",
                "metrics": {},
            }
            if scan["status"] != "available":
                item["error"] = (
                    "扫描仍在规划中" if scan["status"] == "planned" else "扫描被依赖阻断"
                )
            elif template["backtestStrategyId"] == "ma_crossover" and (
                numeric(parameters.get("fast_window")) is None
                or numeric(parameters.get("slow_window")) is None
                or numeric(parameters["fast_window"]) >= numeric(parameters["slow_window"])
            ):
                item["error"] = "fast_window 必须小于 slow_window"
            else:
                try:
                    backtest = await self.market.backtest(
                        target,
                        parameters,
                        template["backtestStrategyId"],
                        template["backtestLimit"],
                    )
                    summary = record(record(backtest).get("summary"))
                    item.update(
                        status="success",
                        metrics={
                            key: numeric(summary.get(raw))
                            for key, raw in (
                                ("totalReturnPct", "total_return_pct"),
                                ("maxDrawdownPct", "max_drawdown_pct"),
                                ("winRatePct", "win_rate_pct"),
                                ("tradeCount", "trade_count"),
                                ("sharpe", "sharpe"),
                            )
                        },
                    )
                except Exception as cause:
                    item.update(status="failed", error=str(cause))
            results.append(item)
        succeeded, failed = (
            sum(item["status"] == "success" for item in results),
            sum(item["status"] == "failed" for item in results),
        )
        best = best_result(results)
        available = scan["status"] == "available"
        run = {
            "id": f"scan-{int(time.time() * 1000)}-{uuid4().hex[:7]}",
            "templateId": template_id,
            "scanId": scan_id,
            "symbol": target,
            "status": "failed" if not succeeded else "partial" if failed else "completed",
            "startedAt": started,
            "completedAt": now(),
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed if available else len(results),
            "bestResultId": best["id"] if best else None,
            "objective": scan["objective"],
            "source": self.market.base
            + "/api/v1/backtests/strategies/"
            + template["backtestStrategyId"]
            + "/{symbol}"
            if available
            else "parameter scan is planned"
            if scan["status"] == "planned"
            else "parameter scan is blocked",
            "results": results,
        }
        await self.scans.save("runs", run)
        return run

    async def enqueue(self, template_id, scan_id, symbol=None):
        template, _ = self.scan_definition(template_id, scan_id)
        timestamp = now()
        job = {
            "id": f"scan-job-{int(time.time() * 1000)}-{uuid4().hex[:7]}",
            "templateId": template_id,
            "scanId": scan_id,
            "symbol": symbol.strip()
            if isinstance(symbol, str) and symbol.strip()
            else (template["defaultSymbols"] or ["510300"])[0],
            "status": "queued",
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "runId": None,
            "error": None,
        }
        await self.scans.save("jobs", job)
        return job

    async def execute_job(self, identity):
        job = await self.scans.claim(identity)
        if job is None:
            return
        job.update(
            status="running", startedAt=job.get("startedAt") or now(), updatedAt=now(), error=None
        )
        await self.scans.save("jobs", job)
        try:
            run = await self.scan(job["templateId"], job["scanId"], job["symbol"])
            job.update(
                status="failed" if run["status"] == "failed" else "completed",
                runId=run["id"],
                completedAt=run["completedAt"],
                updatedAt=now(),
                error="扫描未产生成功结果" if run["status"] == "failed" else None,
            )
        except Exception as cause:
            job.update(status="failed", completedAt=now(), updatedAt=now(), error=str(cause))
        await self.scans.save("jobs", job)

    async def research(self):
        fallback = copy.deepcopy(CATALOG["researchFallback"])
        try:
            response = record(
                await self.market.request("/api/v1/research/universes/summary", timeout=2.5)
            )
            universes = [
                mapped("mapResearchUniverse", value) for value in array(response.get("universes"))
            ]
            primary = (
                next(
                    (item for item in universes if item["id"] == CATALOG["sampleUniverseId"]), None
                )
                or next((item for item in universes if item["stockCount"] > 0), None)
                or (universes or fallback["universes"])[0]
            )
            page = await self.market.members(primary["id"], page=1, page_size=10, timeout=4.5)
            hydrated = [
                {
                    **item,
                    "members": page["members"] if item["id"] == primary["id"] else [],
                    **(
                        {"memberCount": page["total"] or item["memberCount"]}
                        if item["id"] == primary["id"]
                        else {}
                    ),
                }
                for item in universes
            ]
            coverage = [
                {
                    "symbol": member["symbol"],
                    "name": member["name"],
                    "timeframe": primary["defaultTimeframe"],
                    "adjustment": primary["defaultAdjustment"],
                    "provider": member["dataProvider"] or primary["provider"],
                    **{key: member[key] for key in ("firstTs", "lastTs", "rowCount", "dataStatus")},
                }
                for member in page["members"]
            ]
            return {
                **fallback,
                "primaryUniverseId": primary["id"],
                "source": "market-api",
                "universes": hydrated or fallback["universes"],
                "coverage": coverage or fallback["coverage"],
                "ingestionPlan": {
                    **fallback["ingestionPlan"],
                    "universeId": primary["id"],
                    "timeframe": primary["defaultTimeframe"],
                    "adjustment": primary["defaultAdjustment"],
                    "provider": primary["provider"],
                },
                "error": None,
            }
        except Exception as cause:
            return {**fallback, "error": str(cause)}

    async def foundation(self):
        fallback = copy.deepcopy(CATALOG["foundationFallback"])
        responses = await asyncio.gather(
            self.market.request("/api/v1/foundation/status", timeout=2),
            self.market.request("/api/v1/foundation/factors", timeout=2),
            self.market.request(
                "/api/v1/foundation/trading-calendar",
                query={"market": "CN-A", "limit": "30"},
                timeout=2.5,
            ),
            return_exceptions=True,
        )
        failures = [str(value) for value in responses if isinstance(value, Exception)]
        if len(failures) == 3:
            return {**fallback, "error": "；".join(failures) or "基础组件接口暂不可用"}
        status, factors, calendar = [record(value) for value in responses]
        return {
            "source": "market-api",
            "components": [
                mapped("mapFoundationComponent", value) for value in status["components"]
            ]
            if isinstance(status.get("components"), list)
            else fallback["components"],
            "factors": [
                mapped("mapFactorDefinition", value) for value in array(factors.get("factors"))
            ],
            "calendarDays": [
                mapped("mapTradingCalendarDay", value) for value in array(calendar.get("days"))
            ],
            "latestQualityScan": None,
            "error": "；".join(failures) if failures else None,
        }

    async def dashboard(self):
        runs, jobs, research, foundation = await asyncio.gather(
            self.scans.list("runs"), self.scans.list("jobs"), self.research(), self.foundation()
        )
        try:
            rows = await self.projects.database.fetch_all(
                "SELECT * FROM projects ORDER BY created_at DESC"
            )
        except Exception:
            rows = []
        workspaces = []
        for row in rows:
            project = serialize_project(row)
            if project["capabilityId"] in (
                "strategy_research",
                "backtest_review",
                "portfolio_risk",
            ):
                workspaces.append(
                    {
                        key: project[key]
                        for key in (
                            "id",
                            "name",
                            "status",
                            "createdAt",
                            "updatedAt",
                            "capabilityId",
                            "initialPrompt",
                        )
                    }
                )
        templates = []
        for definition in CATALOG["templates"]:
            if not definition.get("kind"):
                continue

            def matches(project, definition=definition):
                text = (project["name"] + " " + (project["initialPrompt"] or "")).lower()
                return (
                    project["capabilityId"] == definition["capabilityId"]
                    or any(symbol.lower() in text for symbol in definition["defaultSymbols"])
                    or any(
                        parameter["key"].lower() in text
                        for parameter in definition["parameterSchema"]
                    )
                )

            templates.append(
                {
                    **copy.deepcopy(definition),
                    "readiness": CATALOG["templateReadiness"][definition["id"]],
                    "linkedWorkspaces": [project for project in workspaces if matches(project)],
                    "latestScanRun": next(
                        (run for run in runs if run["templateId"] == definition["id"]), None
                    ),
                }
            )
        summary = {
            "templates": len(templates),
            "readyTemplates": sum(item["status"] == "ready" for item in templates),
            "plannedTemplates": sum(item["status"] == "planned" for item in templates),
            "strategyWorkspaces": len(workspaces),
            "backtestWorkspaces": sum(
                item["capabilityId"] == "backtest_review" for item in workspaces
            ),
            "dataDependencies": len(
                {dependency for item in templates for dependency in item["dataDependencies"]}
            ),
            "parameterScans": sum(len(item["parameterScans"]) for item in templates),
            "archivedReports": sum(len(item["backtestArchives"]) for item in templates),
            "activeVersions": sum(
                version["status"] == "active" for item in templates for version in item["versions"]
            ),
            "researchUniverses": len(research["universes"]),
            "trackedSymbols": sum(item["memberCount"] for item in research["universes"]),
            "syncedSymbols": sum(item["readyCount"] for item in research["universes"]),
            "syncedBars": sum(item["barCount"] for item in research["universes"]),
        }
        return {
            "generatedAt": now(),
            "summary": summary,
            "templates": templates,
            "workspaces": workspaces,
            "scanRuns": runs,
            "scanJobs": jobs,
            "research": research,
            "foundation": foundation,
            "factorCatalog": CATALOG["factorCatalog"],
        }
