import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .knowledge_growth import KnowledgeGrowth
from .knowledge_repository import KnowledgeGrowthError
from .memory_api import PRIVATE, body_record, string


def route_error(error):
    if isinstance(error, KnowledgeGrowthError) and error.code != "KNOWLEDGE_USE_COLLISION":
        return JSONResponse({"success": False, "error": error.code}, status_code=error.status)
    logging.warning("Governed knowledge API failure (%s)", type(error).__name__)
    return JSONResponse({"success": False, "error": "KNOWLEDGE_FEEDBACK_FAILED"}, status_code=500)


def knowledge_routes(services, growth_factory=None):
    router = APIRouter()

    def growth(database):
        return growth_factory(database) if growth_factory else KnowledgeGrowth(database)

    @router.get("/api/projects/{project_id}/knowledge/uses/{request_id}")
    async def attribution(request: Request, project_id: str, request_id: str):
        database, auth = services()
        await auth.require_action(request, "project.read", project_id)
        try:
            return JSONResponse(
                {"success": True, "data": await growth(database).find(project_id, request_id)},
                headers=PRIVATE,
            )
        except Exception as cause:
            return route_error(cause)

    @router.post("/api/projects/{project_id}/knowledge/outcomes")
    async def outcomes(request: Request, project_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.update", project_id)
        body = await body_record(request)
        outcome = body.get("outcome")
        if not isinstance(outcome, str) or outcome not in {"helped", "neutral", "harmed"}:
            return JSONResponse(
                {"success": False, "error": "INVALID_KNOWLEDGE_FEEDBACK"}, status_code=400
            )
        try:
            data = await growth(database).feedback(
                project_id,
                string(body, "requestId"),
                actor.actor_user_id,
                string(body, "eventId"),
                outcome,
            )
            return JSONResponse({"success": True, "data": data}, status_code=201)
        except Exception as cause:
            return route_error(cause)

    return router
