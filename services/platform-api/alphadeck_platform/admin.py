import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg.errors import UniqueViolation

from .auth import AuthorizationError
from .credentials import AuthApiError, audit
from .quota import one, utc
from .sessions import iso


async def require_admin(authorization, request, options, *, fresh=False):
    user = await authorization.require_session(request)
    if user["role"] != "admin":
        raise AuthorizationError("ADMIN_REQUIRED", 403, "需要管理员权限。")
    if user["must_change_password"]:
        raise AuthorizationError("PASSWORD_CHANGE_REQUIRED", 428, "请先修改初始密码。")
    if fresh and datetime.now(UTC) - utc(user["session_created_at"]) > timedelta(
        seconds=options.session["freshAgeSeconds"]
    ):
        raise AuthorizationError("FRESH_SESSION_REQUIRED", 403, "敏感操作需要重新登录。")
    return user


async def protect_last_admin(connection, target_id):
    # Same key as the original server: mixed-version deployments must serialize together.
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('auth:active-admin-governance',0))"
    )
    target = await one(
        connection, "SELECT role FROM auth_users WHERE id=%s FOR UPDATE", (target_id,)
    )
    if target and target["role"] == "admin":
        count = await one(
            connection,
            "SELECT count(*) AS n FROM auth_users WHERE role='admin' AND (banned=false OR ban_expires<=now())",
        )
        if count["n"] <= 1:
            raise ValueError("不能停用或降级最后一个可用管理员。")


def temporary_password():
    return secrets.token_urlsafe(12) + "Aa1!"


def private(payload, status=200):
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "private, no-store"})


def operation_failed(error):
    return JSONResponse(
        {"success": False, "error": "AUTH_OPERATION_FAILED", "message": str(error)}, status_code=400
    )


async def member_profiles(connection):
    permission = await one(
        connection, "SELECT id FROM permission_profiles WHERE key='member-default'"
    )
    quota = await one(connection, "SELECT id FROM quota_profiles WHERE key='member-default'")
    if not permission or not quota:
        raise ValueError("默认权限或配额模板尚未初始化。")
    return permission["id"], quota["id"]


def positive_integer(value, default, maximum=None):
    found = re.match(r"^[\s]*([+-]?\d+)", value or "")
    result = max(1, int(found[1])) if found else default
    return min(maximum, result) if maximum else result


def admin_routes(services, credentials):
    router = APIRouter()

    @router.get("/api/admin/users")
    async def users(request: Request):
        database, authorization = services()
        await require_admin(authorization, request, credentials().options)
        query = request.query_params.get("q", "").strip()
        page = positive_integer(request.query_params.get("page"), 1)
        size = positive_integer(request.query_params.get("pageSize"), 25, 100)
        where = (
            "WHERE strpos(lower(u.email),lower(%s))>0 OR strpos(lower(u.name),lower(%s))>0"
            if query
            else ""
        )
        parameters = (query, query) if query else ()
        async with database.transaction() as connection:
            await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            total = await one(
                connection, f"SELECT count(*) AS n FROM auth_users u {where}", parameters
            )
            rows = await (
                await connection.execute(
                    f"""SELECT u.*,
                (SELECT count(*) FROM auth_sessions s WHERE s.user_id=u.id) AS sessions,
                (SELECT count(*) FROM project_memberships m WHERE m.user_id=u.id) AS memberships,
                (SELECT count(*) FROM projects p WHERE p.owner_id=u.id) AS owned
                FROM auth_users u {where} ORDER BY role,created_at LIMIT %s OFFSET %s""",
                    parameters + (size, (page - 1) * size),
                )
            ).fetchall()
        output = []
        for row in rows:
            item = {
                name: row[field]
                for name, field in (
                    ("id", "id"),
                    ("name", "name"),
                    ("email", "email"),
                    ("role", "role"),
                    ("banned", "banned"),
                    ("banReason", "ban_reason"),
                    ("mustChangePassword", "must_change_password"),
                )
            }
            for name, field in (
                ("lastLoginAt", "last_login_at"),
                ("passwordChangedAt", "password_changed_at"),
                ("createdAt", "created_at"),
                ("updatedAt", "updated_at"),
            ):
                item[name] = iso(row[field]) if row[field] else None
            item["_count"] = {
                "sessions": row["sessions"],
                "projectMemberships": row["memberships"],
                "ownedProjects": row["owned"],
            }
            output.append(item)
        return private(
            {
                "success": True,
                "data": {"users": output, "total": total["n"], "page": page, "pageSize": size},
            }
        )

    @router.post("/api/admin/users")
    async def create_user(request: Request):
        database, authorization = services()
        service = credentials()
        actor = await require_admin(authorization, request, service.options, fresh=True)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.keys() - {"email", "name", "role", "password"}:
                raise ValueError("Invalid create user request")
            email, name, role = body.get("email"), body.get("name"), body.get("role", "member")
            if (
                not isinstance(email, str)
                or len(email.strip()) > 320
                or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip())
                or not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 120
                or role not in {"member", "admin"}
            ):
                raise ValueError("Invalid user fields")
            initial = body.get("password", temporary_password())
            if not isinstance(initial, str) or not 12 <= len(initial) <= 128:
                raise ValueError("Invalid password length")
            hashed = await service.options.hash_password(initial)
            identity, email = str(uuid4()), email.strip().lower()
            async with database.transaction() as connection:
                permission, quota = (
                    await member_profiles(connection) if role == "member" else (None, None)
                )
                await connection.execute(
                    """INSERT INTO auth_users
                    (id,email,name,role,email_verified,must_change_password,permission_profile_id,quota_profile_id,updated_at)
                    VALUES (%s,%s,%s,%s,true,true,%s,%s,now())""",
                    (identity, email, name.strip(), role, permission, quota),
                )
                await connection.execute(
                    "INSERT INTO auth_accounts (id,provider_id,account_id,user_id,password,updated_at) VALUES (%s,'credential',%s,%s,%s,now())",
                    (str(uuid4()), identity, identity, hashed),
                )
            await audit(
                database,
                request,
                "admin.user_created",
                actor=actor["id"],
                target_type="user",
                target_id=identity,
                metadata={"role": role},
            )
            return private(
                {
                    "success": True,
                    "data": {
                        "userId": identity,
                        "email": email,
                        "initialPassword": initial,
                        "mustChangePassword": True,
                    },
                },
                201,
            )
        except (ValueError, AuthApiError, UniqueViolation) as error:
            return operation_failed(error)

    @router.patch("/api/admin/users")
    async def update_user(request: Request):
        database, authorization = services()
        service = credentials()
        actor = await require_admin(authorization, request, service.options, fresh=True)
        try:
            body = await request.json()
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("userId"), str)
                or not body["userId"]
            ):
                raise ValueError("Invalid user update request")
            action, identity = body.get("action"), body["userId"]
            if action not in {"set-role", "set-status", "reset-password", "revoke-sessions"}:
                raise ValueError("Invalid user update action")
            if action == "set-role" and body.get("role") not in {"member", "admin"}:
                raise ValueError("Invalid role")
            if action == "set-status" and (
                type(body.get("banned")) is not bool
                or (
                    "reason" in body
                    and (not isinstance(body["reason"], str) or len(body["reason"]) > 500)
                )
            ):
                raise ValueError("Invalid status")
            if action == "set-role" and identity == actor["id"]:
                raise ValueError("不能修改自己的管理员角色。")
            if action == "set-status" and body["banned"] and identity == actor["id"]:
                raise ValueError("不能停用自己的账号。")
            initial = temporary_password() if action == "reset-password" else None
            hashed = await service.options.hash_password(initial) if initial else None
            async with database.transaction() as connection:
                # Take the governance lock before any user row: concurrent demotions cannot deadlock or remove all admins.
                if (action == "set-role" and body["role"] != "admin") or (
                    action == "set-status" and body["banned"]
                ):
                    await protect_last_admin(connection, identity)
                target = await one(
                    connection, "SELECT id FROM auth_users WHERE id=%s FOR UPDATE", (identity,)
                )
                if not target:
                    return private({"success": False, "error": "USER_NOT_FOUND"}, 404)
                if action == "set-role":
                    permission, quota = (
                        await member_profiles(connection)
                        if body["role"] == "member"
                        else (None, None)
                    )
                    await connection.execute(
                        "UPDATE auth_users SET role=%s,permission_profile_id=%s,quota_profile_id=%s,access_version=access_version+1,updated_at=now() WHERE id=%s",
                        (body["role"], permission, quota, identity),
                    )
                    await connection.execute(
                        "DELETE FROM user_permission_overrides WHERE user_id=%s", (identity,)
                    )
                    await connection.execute(
                        "DELETE FROM user_quota_overrides WHERE user_id=%s", (identity,)
                    )
                    await connection.execute(
                        "DELETE FROM auth_sessions WHERE user_id=%s", (identity,)
                    )
                    data = {"role": body["role"]}
                elif action == "set-status":
                    await connection.execute(
                        "UPDATE auth_users SET banned=%s,ban_reason=%s,ban_expires=NULL,updated_at=now() WHERE id=%s",
                        (
                            body["banned"],
                            (body.get("reason") or "由管理员停用") if body["banned"] else None,
                            identity,
                        ),
                    )
                    if body["banned"]:
                        await connection.execute(
                            "DELETE FROM auth_sessions WHERE user_id=%s", (identity,)
                        )
                    data = {"banned": body["banned"]}
                elif action == "reset-password":
                    result = await connection.execute(
                        "UPDATE auth_accounts SET password=%s,updated_at=now() WHERE provider_id='credential' AND account_id=%s",
                        (hashed, identity),
                    )
                    if result.rowcount != 1:
                        raise ValueError("Credential account not found")
                    await connection.execute(
                        "UPDATE auth_users SET must_change_password=true,password_changed_at=NULL,updated_at=now() WHERE id=%s",
                        (identity,),
                    )
                    await connection.execute(
                        "DELETE FROM auth_sessions WHERE user_id=%s", (identity,)
                    )
                    data = {"initialPassword": initial, "mustChangePassword": True}
                else:
                    result = await connection.execute(
                        "DELETE FROM auth_sessions WHERE user_id=%s", (identity,)
                    )
                    data = {"revokedCount": result.rowcount}
            metadata = (
                {"role": body["role"]}
                if action == "set-role"
                else {"banned": body["banned"]}
                if action == "set-status"
                else {}
            )
            await audit(
                database,
                request,
                "admin." + action,
                actor=actor["id"],
                target_type="user",
                target_id=identity,
                metadata=metadata,
            )
            return private({"success": True, "data": data})
        except (ValueError, AuthApiError) as error:
            return operation_failed(error)

    return router
