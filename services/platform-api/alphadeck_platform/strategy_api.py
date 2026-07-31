import httpx
from fastapi import APIRouter, BackgroundTasks, Request

from .api_errors import ApiError
from .capability_center import capability_center
from .config import ROOT
from .finance_values import js_string
from .http_input import bounded_json
from .messages import MessageResponse
from .metered_operation import metered_operation
from .projects import Projects
from .strategies import Strategies
from .strategy_market import StrategyMarket

READS = {
    "universe-members",
    "symbol-bars",
    "symbol-dividends",
    "realtime-quote",
    "intraday-bars",
    "ingestion-jobs",
    "sector-capital-flow",
    "technical-indicators",
    "financial-reports",
    "fundamental-indicators",
    "announcements",
    "trading-calendar",
    "resolve-symbol",
}
RUNS = {"run-scan", "run-scan-now", "a-share-screener", "data-quality-scan"}
MANAGE = {
    "add-universe-member",
    "control-ingestion-job",
    "run-ingestion-batch",
    "start-ingestion-autofill",
}
WEIGHTS = {
    "universe-members": 1,
    "symbol-bars": 2,
    "symbol-dividends": 1,
    "realtime-quote": 1,
    "intraday-bars": 2,
    "ingestion-jobs": 1,
    "sector-capital-flow": 3,
    "technical-indicators": 2,
    "financial-reports": 2,
    "fundamental-indicators": 2,
    "announcements": 1,
    "trading-calendar": 1,
    "resolve-symbol": 1,
    "a-share-screener": 10,
    "run-scan": 10,
    "run-scan-now": 10,
    "data-quality-scan": 20,
    "run-ingestion-batch": 50,
    "start-ingestion-autofill": 100,
}


def permission(action):
    if action is None or action == "":
        return "quant.strategy.run"
    if not isinstance(action, str):
        return None
    return (
        "quant.data.read"
        if action in READS
        else "quant.strategy.run"
        if action in RUNS
        else "quant.strategy.manage"
        if action in MANAGE
        else None
    )


def strategy_routes(services, root, *, market_client=None, skills_root=ROOT, scans_root=None):
    router = APIRouter()

    def runtime(database):
        return Strategies(
            Projects(database, root), StrategyMarket(market_client), scans_root=scans_root
        )

    @router.get("/api/quant/capability-center")
    async def capabilities(request: Request):
        _, auth = services()
        await auth.require_action(request, "quant.data.read")
        return MessageResponse(
            {
                "success": True,
                "data": await capability_center(skills_root=skills_root, client=market_client),
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/quant/strategies")
    async def dashboard(request: Request):
        database, auth = services()
        context = await auth.require_action(request, "quant.data.read")

        async def execute(_key):
            return {"success": True, "data": await runtime(database).dashboard()}, 200

        return await metered_operation(
            database,
            context,
            scope="quant-strategies",
            action="dashboard",
            payload={
                "method": "GET",
                "action": "dashboard",
                "body": {},
                "query": dict(request.query_params),
            },
            requested_key=request.headers.get("idempotency-key", ""),
            units=1,
            execute=execute,
        )

    @router.post("/api/quant/strategies")
    async def action(request: Request, background: BackgroundTasks):
        body = await bounded_json(request, 2097152)
        if not isinstance(body, dict):
            raise ApiError(400, "INVALID_STRATEGY_REQUEST", "Expected a strategy request object")
        selected = permission(body.get("action"))
        if selected is None:
            return MessageResponse(
                {"success": False, "error": "Unsupported strategy action"}, status_code=400
            )
        database, auth = services()
        context = await auth.require_action(request, selected)
        operation = runtime(database)
        name = body.get("action") or ""

        async def execute(_key):
            if name in ("run-scan", "run-scan-now"):
                args = (
                    js_string(body.get("templateId") or ""),
                    js_string(body.get("scanId") or ""),
                    body.get("symbol"),
                )
                if name == "run-scan":
                    value = await operation.enqueue(*args)
                    background.add_task(operation.execute_job, value["id"])
                else:
                    value = await operation.scan(*args)
                return {"success": True, "data": value}, 201
            if not name:
                return {
                    "success": True,
                    "data": operation.prompt(
                        js_string(body.get("templateId") or ""), body.get("symbol")
                    ),
                }, 201
            if name == "control-ingestion-job" and body.get(
                "control", body.get("controlAction")
            ) not in ("pause", "resume", "stop"):
                return {"success": False, "error": "Unsupported ingestion control action"}, 400
            try:
                value = await operation.market.action(name, body)
            except (ValueError, httpx.HTTPError, TimeoutError) as error:
                raise ApiError(502, "MARKET_OPERATION_FAILED", str(error)) from error
            status = (
                201
                if name
                in (
                    "add-universe-member",
                    "data-quality-scan",
                    "run-ingestion-batch",
                    "start-ingestion-autofill",
                )
                else 200
            )
            return {"success": True, "data": value}, status

        requested_key = request.headers.get(
            "idempotency-key",
            body.get("idempotencyKey") if isinstance(body.get("idempotencyKey"), str) else "",
        )
        return await metered_operation(
            database,
            context,
            scope="quant-strategies",
            action=name,
            payload={
                "method": "POST",
                "action": name,
                "body": {key: value for key, value in body.items() if key != "idempotencyKey"},
                "query": dict(request.query_params),
            },
            requested_key=requested_key,
            units=WEIGHTS.get(name, 0),
            execute=execute,
        )

    return router
