import math
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from alphadeck_platform.access_management import access_routes
from alphadeck_platform.admin import admin_routes
from alphadeck_platform.api_errors import ApiError
from alphadeck_platform.approvals import ApprovalError, approval_routes
from alphadeck_platform.asset_api import asset_routes
from alphadeck_platform.auth import AuthorizationError, authorization_from_env
from alphadeck_platform.auth_api import auth_routes
from alphadeck_platform.chat_act import act_routes
from alphadeck_platform.config import ROOT, Settings, load_environment
from alphadeck_platform.credentials import AuthApiError, Credentials
from alphadeck_platform.database import Database
from alphadeck_platform.environment_api import environment_routes
from alphadeck_platform.evaluation_api import evaluation_routes
from alphadeck_platform.external_api import external_routes
from alphadeck_platform.external_services import ExternalServiceError
from alphadeck_platform.files import FileBrowserError
from alphadeck_platform.finance_catalog import FinanceCatalog
from alphadeck_platform.frontend_access import BrowserOriginBoundary, frontend_routes
from alphadeck_platform.git_repository import GitFailure
from alphadeck_platform.http_security import SecurityHeaders
from alphadeck_platform.image_assets import ImageAssetError
from alphadeck_platform.knowledge_api import knowledge_routes
from alphadeck_platform.manual_validation import manual_validation_routes
from alphadeck_platform.members import member_routes
from alphadeck_platform.memory_api import memory_routes
from alphadeck_platform.messages import message_routes
from alphadeck_platform.models import availability
from alphadeck_platform.operations_api import operations_routes
from alphadeck_platform.preview import PreviewManager
from alphadeck_platform.preview_api import preview_routes
from alphadeck_platform.project_api import ProjectRequestError, file_response_error, project_routes
from alphadeck_platform.projects import ProjectNotFound, Projects
from alphadeck_platform.quant_api import quant_routes
from alphadeck_platform.quota import QuotaError
from alphadeck_platform.realtime import chat_runtime_routes
from alphadeck_platform.research_api import research_routes
from alphadeck_platform.sessions import iso, list_sessions, revoke_sessions
from alphadeck_platform.settings import GlobalSettings, serialize
from alphadeck_platform.skills_api import skills_routes
from alphadeck_platform.strategy_api import strategy_routes
from alphadeck_platform.token_api import token_routes
from alphadeck_platform.websocket_api import websocket_routes


def create_app(
    database=None,
    authorization=None,
    data_dir: Path | None = None,
    *,
    projects_dir: Path | None = None,
    file_lock_wait_seconds=5,
    project_provisioner=None,
    preview_manager=None,
    query_rewriter=None,
    market_client=None,
    memory_service_factory=None,
    knowledge_growth_factory=None,
    validator_factory=None,
    external_service_factory=None,
    readiness_factory=None,
    skills_root=None,
    skills_administration_factory=None,
    strategy_scans_root=None,
    notification_client=None,
    evaluation_root=None,
    web_dist_dir: Path | None = None,
):
    if database is None:
        load_environment()

    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            if application.state.preview_manager:
                await application.state.preview_manager.cleanup_all()

    application = FastAPI(title="AlphaDeck Platform API", lifespan=lifespan)
    application.state.preview_manager = preview_manager
    application.state.database = database
    application.state.authorization = authorization
    application.state.credentials = None

    @application.exception_handler(ExternalServiceError)
    async def external_error(request, error):
        return JSONResponse(
            {"success": False, "error": error.message, "message": error.message},
            status_code=error.status,
            headers={"Cache-Control": "private, no-store"},
        )

    @application.exception_handler(GitFailure)
    async def git_error(request, error):
        return JSONResponse({"success": False, "error": str(error)}, status_code=502)

    def services():
        if application.state.database is None:
            application.state.database = Database(Settings.from_env().database_url)
        if application.state.authorization is None:
            application.state.authorization = authorization_from_env(application.state.database)
        return application.state.database, application.state.authorization

    def credentials():
        if application.state.credentials is None:
            database, auth = services()
            application.state.credentials = Credentials(database, auth)
        return application.state.credentials

    @application.exception_handler(AuthApiError)
    async def auth_api_error(request, error):
        return JSONResponse(
            {"code": error.code, "message": error.message}, status_code=error.status
        )

    @application.exception_handler(ApiError)
    async def api_error(request, error):
        return JSONResponse(
            {"success": False, "error": error.code, "message": error.message},
            status_code=error.status,
        )

    @application.exception_handler(ImageAssetError)
    async def image_asset_error(request, error):
        return JSONResponse({"success": False, "error": error.message}, status_code=error.status)

    @application.exception_handler(ApprovalError)
    async def approval_error(request, error):
        status = (
            404
            if error.code == "APPROVAL_NOT_FOUND"
            else 400
            if error.code == "INVALID_APPROVAL"
            else 409
        )
        return JSONResponse(
            {"success": False, "error": error.code, "message": error.message}, status_code=status
        )

    @application.exception_handler(ProjectNotFound)
    async def project_not_found(request, error):
        return JSONResponse({"success": False, "error": "Project not found"}, status_code=404)

    @application.exception_handler(ProjectRequestError)
    async def project_request_error(request, error):
        return JSONResponse(
            {
                "success": False,
                "error": "INVALID_PROJECT_REQUEST",
                "message": "项目创建请求必须使用当前 camelCase 合同。"
                if error.creation
                else "项目更新请求必须使用当前 camelCase 合同。",
                **({} if error.creation else {"issues": error.issues}),
            },
            status_code=400,
        )

    @application.exception_handler(QuotaError)
    async def quota_error(request, error):
        if error.decision:
            decision = error.decision
            reset = decision["window"]["end"]
            counter = decision["counter"]
            payload = {
                "metric": decision["policy"]["metric"],
                "enforcement": decision["policy"]["enforcement"],
                **{
                    key: str(counter[key]) if counter[key] is not None else None
                    for key in ("used", "reserved", "requested", "limit", "remaining")
                },
                "resetAt": iso(reset),
            }
            return JSONResponse(
                {
                    "success": False,
                    "error": error.code,
                    "message": "当前操作会超过可用配额，请等待额度重置或联系管理员调整策略。",
                    "quota": payload,
                },
                status_code=429,
                headers={
                    "Retry-After": str(
                        max(1, math.ceil((reset - datetime.now(UTC)).total_seconds()))
                    )
                },
            )
        return JSONResponse(
            {"success": False, "error": error.code, "message": error.message},
            status_code=error.status,
        )

    @application.exception_handler(FileBrowserError)
    async def file_error(request, error):
        return file_response_error(request, error.message, error.status)

    @application.exception_handler(AuthorizationError)
    async def authorization_error(request, error):
        return JSONResponse(
            {"success": False, "error": error.code, "message": error.message},
            status_code=error.status,
        )

    @application.get("/api/health")
    @application.head("/api/health")
    async def health():
        return JSONResponse(
            {"ok": True, "service": "alphadeck-web"},
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @application.get("/api/quant/capabilities")
    async def quant_capabilities(request: Request):
        _, auth = services()
        await auth.require_action(request, "quant.data.read")
        catalog = FinanceCatalog()
        return {
            "success": True,
            "data": {
                "defaultCapabilityId": catalog.data["profile"]["defaultCapabilityId"],
                "capabilities": catalog.serialized_capabilities(),
            },
        }

    @application.get("/api/settings/cli-status")
    async def cli_status(request: Request):
        _, auth = services()
        await auth.require_action(request, "quant.data.read")
        return JSONResponse(
            {"pi": await availability()}, headers={"Cache-Control": "private, no-store"}
        )

    @application.get("/api/settings")
    @application.get("/api/settings/global")
    async def get_settings(request: Request):
        database, auth = services()
        await auth.require_action(request, "quant.data.read")
        try:
            value = await GlobalSettings(database, data_dir or ROOT / "data").load()
        except Exception:
            return JSONResponse({"error": "Failed to load global settings"}, status_code=500)
        return JSONResponse(serialize(value), headers={"Cache-Control": "private, no-store"})

    @application.put("/api/settings")
    @application.put("/api/settings/global")
    async def put_settings(request: Request):
        database, auth = services()
        await auth.require_action(request, "platform.settings.manage")
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {
                    "error": "Failed to update global settings",
                    "message": "Invalid JSON request body",
                },
                status_code=500,
            )
        value = await GlobalSettings(database, data_dir or ROOT / "data").update(body)
        return serialize(value)

    @application.get("/api/account/sessions")
    async def get_account_sessions(request: Request):
        database, auth = services()
        user = await auth.require_session(request)
        return JSONResponse(
            {"success": True, "data": await list_sessions(database, user)},
            headers={"Cache-Control": "private, no-store"},
        )

    @application.delete("/api/account/sessions")
    async def delete_account_sessions(request: Request):
        database, auth = services()
        user = await auth.require_session(request)
        try:
            body = await request.json()
            if (
                not isinstance(body, dict)
                or body.keys() - {"sessionId", "allOthers"}
                or (
                    "sessionId" in body
                    and (not isinstance(body["sessionId"], str) or not body["sessionId"])
                )
                or ("allOthers" in body and type(body["allOthers"]) is not bool)
            ):
                raise ValueError("Invalid session revoke request")
            count = await revoke_sessions(
                database,
                user,
                session_id=body.get("sessionId"),
                all_others=body.get("allOthers", False),
            )
        except ValueError as error:
            return JSONResponse(
                {"success": False, "error": "AUTH_OPERATION_FAILED", "message": str(error)},
                status_code=400,
            )
        return {"success": True, "data": {"revokedCount": count}}

    root = projects_dir or Path(
        os.getenv("PROJECTS_DIR", str((data_dir or ROOT / "data") / "projects"))
    )

    def previews():
        if application.state.preview_manager is None:
            database, _ = services()
            application.state.preview_manager = PreviewManager(Projects(database, root))
        return application.state.preview_manager

    application.include_router(
        project_routes(
            services,
            root,
            lock_wait_seconds=file_lock_wait_seconds,
            provisioner=project_provisioner,
        )
    )
    application.include_router(auth_routes(services, credentials))
    application.include_router(frontend_routes(services, application))
    application.add_middleware(BrowserOriginBoundary, application=application, services=services)
    application.add_middleware(SecurityHeaders)
    application.include_router(admin_routes(services, credentials))
    application.include_router(access_routes(services, credentials))
    application.include_router(member_routes(services))
    application.include_router(memory_routes(services, memory_service_factory))
    application.include_router(knowledge_routes(services, knowledge_growth_factory))
    application.include_router(message_routes(services))
    application.include_router(approval_routes(services))
    application.include_router(chat_runtime_routes(services))
    application.include_router(websocket_routes(services))
    application.include_router(act_routes(services, root))
    application.include_router(asset_routes(services, root))
    application.include_router(environment_routes(services, root))
    application.include_router(token_routes(services))
    application.include_router(evaluation_routes(services, root=evaluation_root or ROOT))
    application.include_router(
        research_routes(
            services, market_client=market_client, notification_client=notification_client
        )
    )
    application.include_router(
        skills_routes(
            services,
            root,
            catalogue_root=skills_root or ROOT,
            administration_factory=skills_administration_factory,
        )
    )
    application.include_router(external_routes(services, root, external_service_factory))
    application.include_router(
        operations_routes(
            services,
            root,
            readiness_factory=readiness_factory,
            market_client=market_client,
            skills_root=skills_root or ROOT,
            scans_root=strategy_scans_root,
        )
    )
    application.include_router(preview_routes(services, previews))
    application.include_router(manual_validation_routes(services, previews, validator_factory))
    application.include_router(
        quant_routes(services, rewriter=query_rewriter, market_client=market_client)
    )
    application.include_router(
        strategy_routes(
            services,
            root,
            market_client=market_client,
            skills_root=skills_root or ROOT,
            scans_root=strategy_scans_root,
        )
    )

    # The production Vue workbench is a static SPA. Keep private data in /api;
    # only compiled public assets are served here.
    web_dist = web_dist_dir or ROOT / "web" / "dist"

    @application.get("/{web_path:path}", include_in_schema=False)
    async def vue_workbench(web_path: str):
        if web_path.startswith(("api/", "internal/")):
            return JSONResponse({"error": "NOT_FOUND"}, status_code=404)
        target = (web_dist / web_path).resolve()
        if web_dist.resolve() not in target.parents or not target.is_file():
            target = web_dist / "index.html"
        if not target.is_file():
            return JSONResponse({"error": "WEB_NOT_BUILT"}, status_code=404)
        response = FileResponse(target)
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
            if web_path.startswith("assets/")
            else "private, no-store"
        )
        return response
    return application


app = create_app()
