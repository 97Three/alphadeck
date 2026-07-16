"""Python owns page admission and browser-origin policy for the Vue workbench."""

import json
import os
import re
from contextlib import suppress
from urllib.parse import unquote

from fastapi import APIRouter, Request, Response

from .auth import AuthorizationError
from .config import ROOT
from .credentials import AuthOptions, audit
from .privileged_request import origin

SAFE = {"GET", "HEAD", "OPTIONS"}
PASSWORD_PATHS = {
    "/account/security",
    "/api/account/password",
    "/api/account/sessions",
    "/api/health",
}
BRAND = {
    "/apple-touch-icon.png",
    "/favicon-16.png",
    "/favicon-32.png",
    "/favicon.png",
    "/favicon.ico",
    "/icon.svg",
    "/manifest.webmanifest",
    "/alphadeck-mark.svg",
    "/AlphaDeck_Icon.png",
}


def same_origin(request, *, strict=False):
    provided = request.headers.get("origin")
    if provided:
        try:
            if origin(provided) in AuthOptions().trusted_origins:
                return True
            host = (
                (
                    request.headers.get("x-forwarded-host")
                    or request.headers.get("host")
                    or request.url.netloc
                )
                .split(",")[0]
                .strip()
            )
            protocol = (
                (request.headers.get("x-forwarded-proto") or request.url.scheme)
                .split(",")[0]
                .strip()
            )
            protocol = {"ws": "http", "wss": "https"}.get(protocol, protocol)
            return origin(provided) == origin(protocol + "://" + host)
        except ValueError:
            return False
        except Exception:
            return False
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    return not strict or request.headers.get("x-alphadeck-request") == "same-origin"


def public_path(path):
    definitions = json.loads((ROOT / "config/auth.json").read_text())["local"]["publicPaths"]
    return (
        path in BRAND
        or bool(re.fullmatch(r"/icons/alphadeck-(?:192|512)\.png", path))
        or path.startswith("/assets/")
        or any(
            path == entry or path.startswith(entry + "/")
            for entry in definitions
            if entry != "/login"
        )
    )


async def page_access(auth, database, request, path, method):
    if not auth.enabled or public_path(path):
        return {"kind": "allow"}
    try:
        user = await auth.session(request)
        if path == "/login":
            return {"kind": "redirect", "location": "/"} if user else {"kind": "allow"}
        if not user:
            if path.startswith("/api/"):
                return {
                    "kind": "deny",
                    "status": 401,
                    "body": {
                        "error": "AUTHENTICATION_REQUIRED",
                        "message": "请先登录 AlphaDeck。",
                    },
                }
            return {"kind": "redirect", "location": "/login", "next": path}
        if (
            user["must_change_password"]
            and path not in PASSWORD_PATHS
            and not path.startswith("/api/auth/")
        ):
            raise AuthorizationError("PASSWORD_CHANGE_REQUIRED", 428, "首次登录必须先修改密码。")
        if (
            path == "/admin" or path.startswith(("/admin/", "/api/admin/")) or path == "/api/admin"
        ) and user["role"] != "admin":
            raise AuthorizationError("ADMIN_REQUIRED", 403, "需要管理员权限。")
        match = re.match(r"^/([^/]+)/chat(?:/|$)", path)
        if match and user["role"] != "admin":
            project = unquote(match[1])
            found = await database.fetch_one(
                "SELECT p.id FROM projects p LEFT JOIN project_memberships m ON m.project_id=p.id AND m.user_id=%s WHERE p.id=%s AND (p.owner_id=%s OR m.role IN ('owner','editor','viewer'))",
                (user["id"], project, user["id"]),
            )
            if not found:
                raise AuthorizationError("PROJECT_NOT_FOUND", 404, "项目不存在或当前用户无权访问。")
        if (
            path.startswith("/api/")
            and method not in SAFE
            and not same_origin(request, strict=True)
        ):
            raise AuthorizationError("INVALID_REQUEST_ORIGIN", 403, "请求来源校验失败。")
        return {"kind": "allow"}
    except AuthorizationError as error:
        if path.startswith("/api/"):
            return {
                "kind": "deny",
                "status": error.status,
                "body": {"error": error.code, "message": error.message},
            }
        if error.code == "PASSWORD_CHANGE_REQUIRED":
            return {"kind": "redirect", "location": "/account/security?required=1"}
        return {
            "kind": "rewrite",
            "location": "/forbidden",
            "status": 404 if error.status == 404 else 403,
        }


def frontend_routes(services, application):
    router = APIRouter()

    @router.get("/internal/frontend/access")
    async def access(request: Request, response: Response):
        response.headers["Cache-Control"] = "private, no-store"
        path, method = (
            request.query_params.get("path", "/"),
            request.query_params.get("method", "GET").upper(),
        )
        if (
            not path.startswith("/")
            or path.startswith("//")
            or len(path) > 8192
            or re.search(r"[\x00-\x1f]", path)
        ):
            return {"kind": "deny", "status": 400, "body": {"error": "INVALID_PATH"}}
        configured = application.state.authorization
        enabled = (
            configured.enabled
            if configured is not None
            else os.getenv("ALPHADECK_AUTH_MODE", "disabled") == "local"
        )
        if not enabled or public_path(path):
            return {"kind": "allow"}
        database, auth = services()
        decision = await page_access(auth, database, request, path, method)
        if decision.get("kind") in ("deny", "rewrite"):
            user = None
            with suppress(AuthorizationError):
                user = await auth.session(request)
            await audit(
                database,
                request,
                "authorization.denied",
                actor=user["id"] if user else None,
                target_type="route",
                target_id=path,
                outcome="denied",
                metadata={"method": method, "status": decision.get("status")},
            )
        return decision

    return router


class BrowserOriginBoundary:
    def __init__(self, app, application, services):
        self.app, self.application, self.services = app, application, services

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["path"].startswith("/api/")
            and scope["method"] not in SAFE
        ):
            configured = self.application.state.authorization
            enabled = (
                configured.enabled
                if configured is not None
                else os.getenv("ALPHADECK_AUTH_MODE", "disabled") == "local"
            )
            if enabled:
                request = Request(scope)
                if not same_origin(request):
                    from starlette.responses import JSONResponse

                    await JSONResponse(
                        {"error": "INVALID_REQUEST_ORIGIN", "message": "请求来源校验失败。"},
                        status_code=403,
                    )(scope, receive, send)
                    return
        await self.app(scope, receive, send)
