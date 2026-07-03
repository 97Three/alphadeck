"""HTTP compatibility for the enabled BetterAuth admin client permissions."""

import re
from uuid import uuid4

from psycopg import sql

from .admin import member_profiles, require_admin
from .credentials import AuthApiError, audit, public_session, public_user
from .quota import advisory, one

ALLOWED = {
    "user": {"create", "list", "set-password", "get", "update"},
    "session": {"list", "revoke", "delete"},
}
FORBIDDEN = {
    "set-role",
    "ban-user",
    "unban-user",
    "remove-user",
    "impersonate-user",
    "stop-impersonating",
}


async def admin_compat(path, request, body, database, authorization, service):
    action = path.removeprefix("/admin/")
    actor = await require_admin(authorization, request, service.options)
    if action in FORBIDDEN:
        raise AuthApiError(
            "FORBIDDEN",
            "The configured administrator permission profile forbids this operation",
            403,
        )
    if action == "has-permission":
        grants = body.get("permissions", body.get("permission", {}))
        role = body.get("role", actor["role"])
        if body.get("userId"):
            row = await database.fetch_one(
                "SELECT role FROM auth_users WHERE id=%s", (body["userId"],)
            )
            role = row["role"] if row else None
        success = (
            role == "admin"
            and isinstance(grants, dict)
            and all(
                isinstance(values, list)
                and all(
                    isinstance(value, str) and value in ALLOWED.get(resource, ())
                    for value in values
                )
                for resource, values in grants.items()
            )
        )
        return {"success": success}
    if action == "list-users" and request.method == "GET":
        query = request.query_params
        try:
            limit, offset = int(query.get("limit", "100")), int(query.get("offset", "0"))
        except ValueError as error:
            raise AuthApiError("VALIDATION_ERROR", "Invalid pagination") from error
        if not 1 <= limit <= 1000 or offset < 0:
            raise AuthApiError("VALIDATION_ERROR", "Invalid pagination")
        conditions, parameters = [], []
        for prefix in ("search", "filter"):
            value = query.get(prefix + "Value")
            if value is None:
                continue
            field = query.get(prefix + "Field", "email")
            fields = {"id": "id", "email": "email", "name": "name", "role": "role"}
            if field not in fields:
                raise AuthApiError("VALIDATION_ERROR", "Unsupported user search field")
            column = sql.Identifier(fields[field])
            operator = query.get(prefix + "Operator", "contains" if prefix == "search" else "eq")
            if operator == "contains":
                conditions.append(sql.SQL("strpos(lower({}),lower(%s))>0").format(column))
            elif operator in ("eq", "ne"):
                conditions.append(
                    sql.SQL("{} " + ("=" if operator == "eq" else "<>") + " %s").format(column)
                )
            elif operator in ("starts_with", "ends_with"):
                function = "starts_with" if operator == "starts_with" else "ends_with"
                conditions.append(sql.SQL(function + "({},%s)").format(column))
            else:
                raise AuthApiError("VALIDATION_ERROR", "Unsupported user search operator")
            parameters.append(value)
        where = (
            sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("")
        )
        fields = {
            "id": "id",
            "name": "name",
            "email": "email",
            "role": "role",
            "createdAt": "created_at",
            "updatedAt": "updated_at",
        }
        sort = fields.get(query.get("sortBy", "createdAt"))
        if sort is None or query.get("sortDirection", "desc") not in ("asc", "desc"):
            raise AuthApiError("VALIDATION_ERROR", "Invalid user ordering")
        direction = sql.SQL(query.get("sortDirection", "desc").upper())
        async with database.transaction() as tx:
            await tx.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            total = await one(
                tx, sql.SQL("SELECT count(*) AS n FROM auth_users") + where, parameters
            )
            rows = await (
                await tx.execute(
                    sql.SQL("SELECT * FROM auth_users")
                    + where
                    + sql.SQL(" ORDER BY {} {},id LIMIT %s OFFSET %s").format(
                        sql.Identifier(sort), direction
                    ),
                    (*parameters, limit, offset),
                )
            ).fetchall()
        return {
            "users": [public_user(row) for row in rows],
            "total": total["n"],
            "limit": limit,
            "offset": offset,
        }
    identity = request.query_params.get("id") if action == "get-user" else body.get("userId")
    if action == "create-user":
        email, name, role = body.get("email"), body.get("name"), body.get("role", "member")
        if (
            not isinstance(email, str)
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
            or not isinstance(name, str)
            or not 1 <= len(name) <= 120
            or role not in ("admin", "member")
        ):
            raise AuthApiError("VALIDATION_ERROR", "Invalid user fields")
        hashed = (
            await service.options.hash_password(body.get("password"))
            if body.get("password") is not None
            else None
        )
        identity = str(uuid4())
        async with database.transaction() as tx:
            await advisory(tx, "auth-sign-up", email.lower())
            if await one(tx, "SELECT id FROM auth_users WHERE email=%s", (email.lower(),)):
                raise AuthApiError(
                    "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL", "User already exists", 422
                )
            permission, quota = await member_profiles(tx) if role == "member" else (None, None)
            row = await one(
                tx,
                "INSERT INTO auth_users (id,email,name,role,permission_profile_id,quota_profile_id,updated_at) VALUES (%s,%s,%s,%s,%s,%s,now()) RETURNING *",
                (identity, email.lower(), name, role, permission, quota),
            )
            if hashed:
                await tx.execute(
                    "INSERT INTO auth_accounts (id,user_id,account_id,provider_id,password,updated_at) VALUES (%s,%s,%s,'credential',%s,now())",
                    (str(uuid4()), identity, identity, hashed),
                )
        await audit(
            database,
            request,
            "admin.user_created",
            actor=actor["id"],
            target_type="user",
            target_id=identity,
        )
        return {"user": public_user(row)}
    if action not in {
        "get-user",
        "update-user",
        "set-user-password",
        "list-user-sessions",
        "revoke-user-session",
        "revoke-user-sessions",
    }:
        raise AuthApiError("NOT_FOUND", "Unknown admin API", 404)
    if action == "revoke-user-session":
        if not isinstance(body.get("sessionToken"), str):
            raise AuthApiError("VALIDATION_ERROR", "Session token is required")
        async with database.transaction() as tx:
            await tx.execute("DELETE FROM auth_sessions WHERE token=%s", (body["sessionToken"],))
        return {"success": True}
    if not isinstance(identity, str) or not identity:
        raise AuthApiError("VALIDATION_ERROR", "User ID is required")
    user = await database.fetch_one("SELECT * FROM auth_users WHERE id=%s", (identity,))
    if not user:
        raise AuthApiError("USER_NOT_FOUND", "User not found", 404)
    if action == "get-user":
        return public_user(user)
    if action == "list-user-sessions":
        rows = await database.fetch_all(
            "SELECT s.id AS session_id,s.token AS session_token,s.created_at AS session_created_at,s.updated_at AS session_updated_at,s.expires_at AS session_expires_at,s.impersonated_by AS session_impersonated_by,s.ip_address,s.user_agent,u.* FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE s.user_id=%s AND s.expires_at>now()",
            (identity,),
        )
        return {"sessions": [public_session(row) for row in rows]}
    if action == "revoke-user-sessions":
        async with database.transaction() as tx:
            await tx.execute("DELETE FROM auth_sessions WHERE user_id=%s", (identity,))
        return {"success": True}
    if action == "set-user-password":
        hashed = await service.options.hash_password(body.get("newPassword"))
        async with database.transaction() as tx:
            await tx.execute(
                "INSERT INTO auth_accounts (id,user_id,account_id,provider_id,password,updated_at) VALUES (%s,%s,%s,'credential',%s,now()) ON CONFLICT(provider_id,account_id) DO UPDATE SET password=excluded.password,updated_at=now()",
                (str(uuid4()), identity, identity, hashed),
            )
            await tx.execute(
                "UPDATE auth_users SET password_changed_at=now(),updated_at=now() WHERE id=%s",
                (identity,),
            )
        await audit(
            database,
            request,
            "admin.password_changed",
            actor=actor["id"],
            target_type="user",
            target_id=identity,
        )
        return {"status": True}
    data = body.get("data")
    if not isinstance(data, dict) or not data:
        raise AuthApiError("VALIDATION_ERROR", "User data is required")
    if data.keys() & {"role", "banned", "banReason", "banExpires"}:
        raise AuthApiError(
            "FORBIDDEN", "Role and account status require dedicated governance APIs", 403
        )
    fields = {
        "name": "name",
        "image": "image",
        "email": "email",
        "emailVerified": "email_verified",
        "mustChangePassword": "must_change_password",
    }
    if data.keys() - fields.keys() or any(
        (
            type(value) is not bool
            if key in ("emailVerified", "mustChangePassword")
            else not isinstance(value, str) and not (key == "image" and value is None)
        )
        for key, value in data.items()
    ):
        raise AuthApiError("VALIDATION_ERROR", "Unsupported user field")
    if "email" in data and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", data["email"]):
        raise AuthApiError("INVALID_EMAIL", "Invalid email")
    async with database.transaction() as tx:
        row = await one(
            tx,
            sql.SQL("UPDATE auth_users SET {},updated_at=now() WHERE id=%s RETURNING *").format(
                sql.SQL(",").join(
                    sql.SQL("{}=%s").format(sql.Identifier(fields[key])) for key in data
                )
            ),
            (*data.values(), identity),
        )
    await audit(
        database,
        request,
        "admin.user_updated",
        actor=actor["id"],
        target_type="user",
        target_id=identity,
    )
    return {"user": public_user(row)}
