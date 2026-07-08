from fastapi import APIRouter, Request

from .api_errors import ApiError
from .config import ROOT
from .evaluation_platform import EvaluationPlatform
from .evaluation_queue import strings
from .http_input import bounded_json
from .messages import MessageResponse
from .privileged_request import privileged_mutation


def evaluation_routes(services, *, root=ROOT):
    router = APIRouter()

    @router.get("/api/evals")
    async def dashboard(request: Request):
        database, auth = services()
        await auth.require_action(request, "platform.observability.read")
        return MessageResponse(
            {"success": True, "data": await EvaluationPlatform(database, root).dashboard()},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/evals/runs/{runId}")
    async def run(request: Request, runId: str):
        database, auth = services()
        await auth.require_action(request, "platform.observability.read")
        value = await EvaluationPlatform(database, root).run(runId)
        return MessageResponse(
            {"success": True, "data": value}
            if value
            else {"success": False, "error": "未找到评测报告。"},
            status_code=200 if value else 404,
            headers={"Cache-Control": "private, no-store"},
        )

    @router.post("/api/evals")
    async def action(request: Request):
        database, auth = services()
        await auth.require_action(request, "platform.settings.manage")
        privileged_mutation(request)
        body = await bounded_json(request, 2097152)
        if not isinstance(body, dict):
            raise ApiError(
                400, "INVALID_EVALUATION_REQUEST", "Expected an evaluation request object"
            )
        selected, service = body.get("action"), EvaluationPlatform(database, root)
        try:
            if selected in ("start-benchmark", "simulate-flow"):
                options = {
                    key: body[key]
                    for key in ("cli", "model", "reasoningEffort", "evaluatorId")
                    if isinstance(body.get(key), str)
                }
                options.update(
                    {
                        key: body[key]
                        for key in ("concurrency", "repeat", "limit")
                        if type(body.get(key)) in (int, float)
                    }
                )
                options.update(
                    mode="e2e" if body.get("mode") == "e2e" else "contract",
                    selectedCases=strings(body.get("selectedCases")),
                    keepProjects=bool(body.get("keepProjects")),
                )
                value = (
                    await service.queue.start(options)
                    if selected == "start-benchmark"
                    else await service.simulate(options)
                )
            elif selected == "create-case":
                value = await service.cases.create(body)
            elif selected == "create-eval-set":
                value = await service.cases.create_set(body)
            elif selected == "cancel-benchmark":
                value = await service.queue.cancel(str(body.get("queueId") or ""))
            elif selected == "update-schedule":
                value = await service.queue.update_schedule(body)
            elif selected == "check-schedule":
                value = await service.queue.check_schedule()
            else:
                return MessageResponse(
                    {"success": False, "error": "不支持的评测 action。"}, status_code=400
                )
        except ValueError as error:
            raise ApiError(400, "EVALUATION_OPERATION_FAILED", str(error)) from error
        return MessageResponse({"success": True, "data": value})

    return router
