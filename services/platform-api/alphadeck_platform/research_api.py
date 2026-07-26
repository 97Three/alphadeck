from fastapi import APIRouter, Request

from .api_errors import ApiError
from .http_input import bounded_json
from .messages import MessageResponse
from .metered_operation import metered_operation
from .privileged_request import privileged_mutation
from .research_reports import ResearchReports


def research_routes(services, *, market_client=None, notification_client=None):
    router = APIRouter()

    def runtime(database):
        return ResearchReports(
            database, market_client=market_client, notification_client=notification_client
        )

    @router.get("/api/research/reports")
    async def dashboard(request: Request):
        database, auth = services()
        await auth.require_action(request, "research.report.read")
        return MessageResponse({"success": True, "data": await runtime(database).dashboard()})

    @router.post("/api/research/reports")
    async def action(request: Request):
        database, auth = services()
        if not auth.enabled:
            privileged_mutation(request)
        body = await bounded_json(request, 2097152)
        if not isinstance(body, dict):
            raise ApiError(400, "INVALID_RESEARCH_REQUEST", "Expected a research request object")
        selected = body.get("action") or "run-daily-report"
        if selected not in ("run-daily-report", "send-latest-report"):
            raise ApiError(400, "INVALID_RESEARCH_ACTION", "Invalid research report action")
        context = await auth.require_action(
            request,
            "research.report.run" if selected == "run-daily-report" else "research.report.send",
        )
        sending = selected == "send-latest-report"
        dry = body.get("dryRun", not sending)
        if type(dry) is not bool:
            raise ApiError(400, "INVALID_RESEARCH_REQUEST", "dryRun must be a boolean")
        if sending and not dry:
            if body.get("confirmed") is not True:
                raise ApiError(400, "RESEARCH_CONFIRMATION_REQUIRED", "真实推送必须显式确认。")
            if (
                not isinstance(body.get("idempotencyKey"), str)
                or not body["idempotencyKey"].strip()
            ):
                raise ApiError(400, "RESEARCH_IDEMPOTENCY_REQUIRED", "真实推送必须提供幂等键。")
        requested_key = body.get("idempotencyKey") or request.headers.get("idempotency-key", "")
        if not isinstance(requested_key, str):
            raise ApiError(400, "INVALID_RESEARCH_REQUEST", "idempotencyKey must be a string")

        async def execute(key):
            service = runtime(database)
            try:
                value = (
                    await service.send(
                        report_id=body.get("reportId"),
                        dry_run=dry,
                        idempotency_key=f"{context.actor_user_id if context.user else 'anonymous'}:{key}",
                    )
                    if sending
                    else await service.run_daily(watchlist_id=body.get("watchlistId"), dry_run=dry)
                )
            except ValueError as error:
                raise ApiError(400, "RESEARCH_OPERATION_FAILED", str(error)) from error
            return {"success": True, "data": value}, 201

        return await metered_operation(
            database,
            context,
            scope="research-reports",
            action=selected,
            payload={
                "action": selected,
                "body": {key: value for key, value in body.items() if key != "idempotencyKey"},
            },
            requested_key=requested_key,
            units=0 if sending and dry else 1,
            metric="research.report_sends.daily" if sending else "research.report_runs.daily",
            source_type="research_report_send" if sending else "research_report",
            execute=execute,
        )

    return router
