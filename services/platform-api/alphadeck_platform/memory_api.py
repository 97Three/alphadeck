import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .auth import AuthorizationError
from .credentials import audit
from .integration_config import memory_config
from .memory_http import MemoryHttpError
from .memory_repository import MemoryRepository
from .memory_service import MemoryIntegrationError, MemoryService
from .messages import js_length
from .sessions import iso

PRIVATE = {"Cache-Control": "private, no-store"}


def serialized_dates(value):
    return {key: iso(item) if isinstance(item, datetime) else item for key, item in value.items()}


def memory_error(error):
    if isinstance(error, AuthorizationError):
        raise error
    if isinstance(error, MemoryIntegrationError):
        return JSONResponse(
            {"success": False, "error": error.code, "message": str(error)}, status_code=error.status
        )
    if isinstance(error, MemoryHttpError):
        return JSONResponse(
            {"success": False, "error": error.code, "requestId": error.request_id},
            status_code=error.status if error.status in {400, 404, 409, 422} else 502,
        )
    logging.warning("Unexpected personal memory API failure (%s)", type(error).__name__)
    return JSONResponse({"success": False, "error": "MEMORY_INTEGRATION_ERROR"}, status_code=500)


async def body_record(request):
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def string(body, key, default=""):
    return body[key] if isinstance(body.get(key), str) else default


def memory_routes(services, service_factory=None):
    router = APIRouter()

    def service(database, config=None):
        return (
            service_factory(database, config)
            if service_factory
            else MemoryService(database, config=config)
        )

    @router.get("/api/account/memory")
    async def account_memory(request: Request):
        database, auth = services()
        user = await auth.require_session(request)
        repository = MemoryRepository(database)
        control, summary = (
            await repository.control(user["id"]),
            await repository.summary(user["id"]),
        )
        configuration_error, config = None, None
        try:
            config = memory_config()
        except Exception:
            configuration_error = "MEMORY_CONFIGURATION_INVALID"
        status = "unavailable" if config is None or config["enabled"] else "disabled"
        info, preferences, error = None, None, configuration_error
        if config and config["enabled"]:
            try:
                runtime = service(database, config)
                info = await runtime.inspect("alphadeck-account-memory")
                preferences = await runtime.list_preferences(
                    user["id"], "alphadeck-account-memory"
                )
                status = "ready"
            except Exception as cause:
                error = (
                    cause.code
                    if isinstance(cause, (MemoryIntegrationError, MemoryHttpError))
                    else "MEMORY_INTEGRATION_ERROR"
                )
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "control": serialized_dates(control),
                    "integration": {
                        "configurationValid": config is not None,
                        "enabled": config["enabled"] if config else False,
                        "required": config["required"] if config else False,
                        "requireProductionReady": config["requireProductionReady"]
                        if config
                        else False,
                        "status": status,
                        "error": error,
                        "service": info,
                    },
                    "preferences": preferences,
                    "valueSummary": serialized_dates(summary),
                    "lifecycle": {
                        "productUsageFenceAvailable": True,
                        "providerErasureAvailable": False,
                        "notice": "关闭后 AlphaDeck 不再把外部记忆用于新任务，但不会删除 Memory 服务中已经保存的数据。",
                    },
                },
            },
            headers=PRIVATE,
        )

    @router.put("/api/account/memory")
    async def update_memory_control(request: Request):
        database, auth = services()
        user = await auth.require_session(request)
        try:
            body = await request.json()
            if (
                not isinstance(body, dict)
                or set(body) != {"personalizationEnabled"}
                or type(body["personalizationEnabled"]) is not bool
            ):
                raise ValueError("Invalid personal memory control update")
        except ValueError as cause:
            return JSONResponse(
                {"success": False, "error": "AUTH_OPERATION_FAILED", "message": str(cause)},
                status_code=400,
            )
        control = await MemoryRepository(database).set_control(
            user["id"], body["personalizationEnabled"]
        )
        await audit(
            database,
            request,
            "personal_memory.enabled"
            if body["personalizationEnabled"]
            else "personal_memory.disabled",
            actor=user["id"],
            target_type="personal_memory_control",
            target_id=user["id"],
            metadata={"changed": control["changed"], "policyVersion": control["policyVersion"]},
        )
        return JSONResponse({"success": True, "data": serialized_dates(control)}, headers=PRIVATE)

    @router.get("/api/projects/{project_id}/memory/preferences")
    async def preferences(request: Request, project_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.read", project_id)
        try:
            data = await service(database).list_preferences(
                actor.actor_user_id, request.headers.get("x-request-id")
            )
            return JSONResponse({"success": True, "data": data}, headers=PRIVATE)
        except Exception as cause:
            return memory_error(cause)

    @router.post("/api/projects/{project_id}/memory/preferences")
    async def remember(request: Request, project_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.update", project_id)
        body = await body_record(request)
        scope = body.get("scope", "global")
        if not isinstance(scope, str) or scope not in {"global", "project"}:
            return JSONResponse(
                {"success": False, "error": "INVALID_MEMORY_SCOPE"}, status_code=400
            )
        try:
            data = await service(database).remember(
                project_id,
                actor.actor_user_id,
                string(body, "eventId"),
                string(body, "key"),
                string(body, "value"),
                string(body, "evidenceText"),
                confidence=body["confidence"]
                if type(body.get("confidence")) in {int, float}
                else 0.95,
                scope=scope,
                context=body.get("context") if isinstance(body.get("context"), dict) else {},
                occurred_at=string(body, "occurredAt", None),
            )
            return JSONResponse({"success": True, "data": data}, status_code=201)
        except Exception as cause:
            return memory_error(cause)

    @router.get("/api/projects/{project_id}/memory/uses/{request_id}")
    async def attribution(request: Request, project_id: str, request_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.read", project_id)
        try:
            data = await service(database).use_attribution(
                project_id, actor.actor_user_id, request_id
            )
            return JSONResponse({"success": True, "data": data}, headers=PRIVATE)
        except Exception as cause:
            return memory_error(cause)

    @router.post("/api/projects/{project_id}/memory/outcomes")
    async def outcome(request: Request, project_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.update", project_id)
        body = await body_record(request)
        if not isinstance(body.get("kind"), str) or body["kind"] not in {
            "helpful",
            "accepted",
            "harmful",
            "rejected",
            "corrected",
        }:
            return JSONResponse(
                {"success": False, "error": "INVALID_MEMORY_OUTCOME"}, status_code=400
            )
        try:
            data = await service(database).feedback(
                project_id,
                actor.actor_user_id,
                string(body, "requestId"),
                string(body, "revisionId"),
                string(body, "eventId"),
                body["kind"],
                weight=body["weight"] if type(body.get("weight")) in {int, float} else 1,
                note=string(body, "note", None),
                occurred_at=string(body, "occurredAt", None),
            )
            return JSONResponse({"success": True, "data": data}, status_code=201)
        except Exception as cause:
            return memory_error(cause)

    @router.get("/api/projects/{project_id}/memory/preferences/{record_id}/revisions")
    async def project_revisions(request: Request, project_id: str, record_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.read", project_id)
        try:
            data = await service(database).revisions(
                actor.actor_user_id, record_id, request.headers.get("x-request-id")
            )
            return JSONResponse({"success": True, "data": data}, headers=PRIVATE)
        except Exception as cause:
            return memory_error(cause)

    @router.post("/api/projects/{project_id}/memory/preferences/{record_id}/corrections")
    async def project_correction(request: Request, project_id: str, record_id: str):
        database, auth = services()
        actor = await auth.require_action(request, "project.update", project_id)
        body = await body_record(request)
        try:
            data = await service(database).correct(
                actor.actor_user_id,
                record_id,
                string(body, "eventId"),
                string(body, "value"),
                string(body, "evidenceText"),
                string(body, "reason"),
                expected_revision=string(body, "expectedRevisionId", None),
                occurred_at=string(body, "occurredAt", None),
            )
            return JSONResponse({"success": True, "data": data}, status_code=201)
        except Exception as cause:
            return memory_error(cause)

    @router.get("/api/account/memory/preferences/{record_id}/revisions")
    async def account_revisions(request: Request, record_id: str):
        database, auth = services()
        user = await auth.require_session(request)
        try:
            data = await service(database).revisions(
                user["id"], record_id, request.headers.get("x-request-id")
            )
            return JSONResponse({"success": True, "data": data}, headers=PRIVATE)
        except Exception as cause:
            return memory_error(cause)

    @router.post("/api/account/memory/preferences/{record_id}/corrections")
    async def account_correction(request: Request, record_id: str):
        database, auth = services()
        user = await auth.require_session(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.keys() - {
                "eventId",
                "value",
                "evidenceText",
                "reason",
                "expectedRevisionId",
                "occurredAt",
            }:
                raise ValueError
            for key, maximum in (
                ("eventId", 160),
                ("value", 4096),
                ("evidenceText", 16384),
                ("reason", 2048),
            ):
                if (
                    not isinstance(body.get(key), str)
                    or not 1 <= js_length(body[key].strip()) <= maximum
                ):
                    raise ValueError
                body[key] = body[key].strip()
            if "expectedRevisionId" in body:
                if (
                    not isinstance(body["expectedRevisionId"], str)
                    or not 1 <= js_length(body["expectedRevisionId"].strip()) <= 64
                ):
                    raise ValueError
                body["expectedRevisionId"] = body["expectedRevisionId"].strip()
            if "occurredAt" in body and (
                not isinstance(body["occurredAt"], str)
                or "T" not in body["occurredAt"]
                or datetime.fromisoformat(body["occurredAt"].replace("Z", "+00:00")).tzinfo is None
            ):
                raise ValueError
        except (ValueError, TypeError):
            return JSONResponse(
                {"success": False, "error": "INVALID_MEMORY_CORRECTION"}, status_code=400
            )
        try:
            data = await service(database).correct(
                user["id"],
                record_id,
                body["eventId"],
                body["value"],
                body["evidenceText"],
                body["reason"],
                expected_revision=body.get("expectedRevisionId"),
                occurred_at=body.get("occurredAt"),
            )
            await audit(
                database,
                request,
                "personal_memory.preference_corrected",
                actor=user["id"],
                target_type="personal_memory_preference",
                target_id=record_id,
                metadata={key: data[key] for key in ("revisionId", "sequence", "idempotentReplay")},
            )
            return JSONResponse({"success": True, "data": data}, status_code=201, headers=PRIVATE)
        except Exception as cause:
            return memory_error(cause)

    return router
