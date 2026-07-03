import hashlib
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .auth import AuthorizationError
from .credentials import AuthApiError, audit, public_session, public_user
from .quota import advisory, one, utc


def auth_routes(services, credentials):
    router = APIRouter()

    @router.get("/api/auth/{all:path}")
    @router.post("/api/auth/{all:path}")
    async def dispatch(request: Request, all: str):
        database, authorization = services()
        if not authorization.enabled:
            return JSONResponse(
                {"error": "AUTH_DISABLED", "message": "AlphaDeck 登录能力当前未启用。"},
                status_code=404,
            )
        service = credentials()
        path = "/" + all.rstrip("/")
        if request.method == "POST":
            service.check_origin(request)
        limited = await service.rate_limit(request, path)
        if limited:
            return limited
        if request.method == "GET":
            if path.startswith("/admin/"):
                from .auth_admin_compat import admin_compat

                return await admin_compat(path, request, {}, database, authorization, service)
            if path == "/get-session":
                response = await service.session_response(request)
                response.headers["Cache-Control"] = "private, no-store"
                return response
            if path == "/ok":
                return {"ok": True}
            if path == "/list-sessions":
                user = await service_session(authorization, request)
                if datetime.now(UTC) - utc(user["session_created_at"]) >= timedelta(
                    seconds=service.options.session["freshAgeSeconds"]
                ):
                    raise AuthApiError("SESSION_NOT_FRESH", "Session is not fresh", 403)
                rows = await database.fetch_all(
                    "SELECT s.id AS session_id,s.token AS session_token,s.created_at AS session_created_at,s.updated_at AS session_updated_at,s.expires_at AS session_expires_at,s.impersonated_by AS session_impersonated_by,s.ip_address,s.user_agent,u.* FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE s.user_id=%s AND s.expires_at>now()",
                    (user["id"],),
                )
                return [public_session(row) for row in rows]
            return JSONResponse(None, status_code=404)
        body = {}
        if await request.body():
            try:
                body = await request.json()
            except ValueError as error:
                raise AuthApiError("VALIDATION_ERROR", "Invalid JSON request") from error
            if not isinstance(body, dict):
                raise AuthApiError("VALIDATION_ERROR", "Body must be an object")
        if path == "/sign-in/email":
            email = body.get("email", "")
            identity = (
                hashlib.sha256(email.strip().lower().encode()).hexdigest()[:20]
                if isinstance(email, str) and email
                else None
            )
            try:
                response, actor = await service.sign_in(request, body)
            except AuthApiError as error:
                await audit(
                    database,
                    request,
                    "authentication.login_failure",
                    target_type="user_identity",
                    target_id=identity,
                    outcome="failure",
                    metadata={"status": error.status},
                )
                raise
            await audit(
                database,
                request,
                "authentication.login_success",
                actor=actor,
                target_type="user_identity",
                target_id=identity,
                metadata={"status": response.status_code},
            )
            return response
        if path.startswith("/admin/"):
            from .auth_admin_compat import admin_compat

            return await admin_compat(path, request, body, database, authorization, service)
        if path == "/sign-out":
            return await service.sign_out(request)
        if path == "/sign-up/email":
            if not service.options.allow_signup:
                raise AuthApiError(
                    "EMAIL_PASSWORD_SIGN_UP_DISABLED", "Email and password sign up is not enabled"
                )
            email, name, password = body.get("email"), body.get("name"), body.get("password")
            if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                raise AuthApiError("INVALID_EMAIL", "Invalid email")
            if not isinstance(name, str) or not name.strip():
                raise AuthApiError("VALIDATION_ERROR", "Name is required")
            image = body.get("image")
            if image is not None and not isinstance(image, str):
                raise AuthApiError("VALIDATION_ERROR", "Invalid image")
            email = email.strip().lower()
            password_hash = await service.options.hash_password(password)
            user_id = str(uuid4())
            async with database.transaction() as connection:
                await advisory(connection, "auth-sign-up", email)
                if await one(connection, "SELECT id FROM auth_users WHERE email=%s", (email,)):
                    raise AuthApiError(
                        "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL",
                        "User already exists. Use another email.",
                        422,
                    )
                user = await one(
                    connection,
                    "INSERT INTO auth_users (id,name,email,image,role,updated_at) VALUES (%s,%s,%s,%s,'member',now()) RETURNING *",
                    (user_id, name, email, image),
                )
                await connection.execute(
                    "INSERT INTO auth_accounts (id,account_id,provider_id,user_id,password,updated_at) VALUES (%s,%s,'credential',%s,%s,now())",
                    (str(uuid4()), user_id, user_id, password_hash),
                )
            return {"token": None, "user": public_user(user)}
        if path in {"/delete-user", "/delete-user/callback"}:
            return JSONResponse(None, status_code=404)
        if path in {"/revoke-session", "/revoke-sessions", "/revoke-other-sessions"}:
            user = await service_session(authorization, request)
            async with database.transaction() as connection:
                if path == "/revoke-session":
                    if not isinstance(body.get("token"), str):
                        raise AuthApiError("VALIDATION_ERROR", "Session token is required")
                    await connection.execute(
                        "DELETE FROM auth_sessions WHERE user_id=%s AND token=%s",
                        (user["id"], body["token"]),
                    )
                elif path == "/revoke-sessions":
                    await connection.execute(
                        "DELETE FROM auth_sessions WHERE user_id=%s", (user["id"],)
                    )
                else:
                    await connection.execute(
                        "DELETE FROM auth_sessions WHERE user_id=%s AND id<>%s AND expires_at>now()",
                        (user["id"], user["session_id"]),
                    )
            return {"status": True}
        if path == "/update-user":
            user = await service_session(authorization, request)
            if body.get("email"):
                raise AuthApiError("EMAIL_CAN_NOT_BE_UPDATED", "Email cannot be updated")
            if "name" not in body and "image" not in body:
                raise AuthApiError("BAD_REQUEST", "No fields to update")
            name, image = body.get("name", user["name"]), body.get("image", user["image"])
            if not isinstance(name, str) or (image is not None and not isinstance(image, str)):
                raise AuthApiError("VALIDATION_ERROR", "Invalid user fields")
            async with database.transaction() as connection:
                await connection.execute(
                    "UPDATE auth_users SET name=%s,image=%s,updated_at=now() WHERE id=%s",
                    (name, image, user["id"]),
                )
            return {"status": True}
        if path == "/change-password":
            user = await service_session(authorization, request)
            revoke = body.get("revokeOtherSessions", False)
            if type(revoke) is not bool:
                raise AuthApiError("VALIDATION_ERROR", "Invalid revokeOtherSessions")
            changed, new_session = await service.change_password(
                request,
                user,
                body.get("currentPassword"),
                body.get("newPassword"),
                revoke_others=revoke,
                rotate_session=revoke,
            )
            response = JSONResponse({"token": None, "user": public_user(changed)})
            if revoke:
                response = JSONResponse(
                    {"token": new_session["token"], "user": public_user(changed)}
                )
                service.set_session_cookie(response, new_session["token"], True)
            return response
        if path == "/verify-password":
            user = await service_session(authorization, request)
            row = await database.fetch_one(
                "SELECT password FROM auth_accounts WHERE user_id=%s AND provider_id='credential' LIMIT 1",
                (user["id"],),
            )
            if not row or not await service.options.verify(row["password"], body.get("password")):
                raise AuthApiError("INVALID_PASSWORD", "Invalid password")
            return {"status": True}
        return JSONResponse(None, status_code=404)

    @router.post("/api/account/password")
    async def account_password(request: Request):
        database, authorization = services()
        user = await authorization.require_session(request)
        service = credentials()
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.keys() != {
                "currentPassword",
                "newPassword",
                "confirmPassword",
            }:
                raise ValueError("Invalid password request")
            if (
                not isinstance(body["currentPassword"], str)
                or not 1 <= len(body["currentPassword"]) <= 256
                or not isinstance(body["newPassword"], str)
                or not 12 <= len(body["newPassword"]) <= 128
                or not isinstance(body["confirmPassword"], str)
                or not 1 <= len(body["confirmPassword"]) <= 128
            ):
                raise ValueError("Invalid password length")
            if body["newPassword"] != body["confirmPassword"]:
                return JSONResponse(
                    {
                        "success": False,
                        "error": "PASSWORD_CONFIRMATION_MISMATCH",
                        "message": "两次输入的新密码不一致。",
                    },
                    status_code=400,
                )
            if body["currentPassword"] == body["newPassword"]:
                return JSONResponse(
                    {
                        "success": False,
                        "error": "PASSWORD_UNCHANGED",
                        "message": "新密码不能与当前密码相同。",
                    },
                    status_code=400,
                )
            await service.change_password(
                request, user, body["currentPassword"], body["newPassword"]
            )
            return {"success": True}
        except (ValueError, AuthApiError) as error:
            await audit(
                database,
                request,
                "user.password_change_failed",
                actor=user["id"],
                target_type="user",
                target_id=user["id"],
                outcome="failure",
                metadata={"reason": type(error).__name__},
            )
            return JSONResponse(
                {"success": False, "error": "AUTH_OPERATION_FAILED", "message": str(error)},
                status_code=400,
            )

    return router


async def service_session(authorization, request):
    try:
        return await authorization.require_session(request)
    except AuthorizationError as error:
        if error.status == 401:
            raise AuthApiError("UNAUTHORIZED", "Unauthorized", 401) from error
        if error.code == "ACCOUNT_DISABLED":
            raise AuthApiError("BANNED_USER", error.message, error.status) from error
        raise
