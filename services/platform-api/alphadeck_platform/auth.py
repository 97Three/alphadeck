import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote

from fastapi import Request

from .permissions import evaluate_permission


class AuthorizationError(Exception):
    def __init__(self, code: str, status: int, message: str):
        self.code, self.status, self.message = code, status, message
        super().__init__(message)


def verify_cookie(value: str, secret: str) -> str | None:
    decoded = unquote(value)
    token, separator, signature = decoded.rpartition(".")
    if not token or not separator or len(signature) != 44 or len(token) > 4096:
        return None
    expected = base64.b64encode(
        hmac.digest(secret.encode(), token.encode(), hashlib.sha256)
    ).decode()
    return token if hmac.compare_digest(expected, signature) else None


def active_ban(user: dict, now: datetime) -> bool:
    if not user.get("banned"):
        return False
    expires = user.get("ban_expires")
    return expires is None or expires.replace(tzinfo=UTC) > now


@dataclass(frozen=True)
class ActionContext:
    actor_user_id: str
    user: dict | None
    project_role: str | None
    local_system_admin: bool


class Authorization:
    def __init__(self, database, *, enabled: bool, secret: str):
        if enabled and not secret:
            raise ValueError("Auth requires ALPHADECK_AUTH_SECRET")
        self.database, self.enabled, self.secret = database, enabled, secret

    async def session(self, request: Request):
        value = request.cookies.get("__Secure-alphadeck.session_token") or request.cookies.get(
            "alphadeck.session_token"
        )
        token = verify_cookie(value, self.secret) if value else None
        if token is None:
            return None
        row = await self.database.fetch_one(
            """SELECT s.id AS session_id, s.created_at AS session_created_at,
                      s.updated_at AS session_updated_at, s.expires_at AS session_expires_at,
                      s.ip_address, s.user_agent, s.token AS session_token,
                      s.impersonated_by AS session_impersonated_by, u.*
               FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id
               WHERE s.token=%s AND s.expires_at > now()""",
            (token,),
        )
        if row and active_ban(row, datetime.now(UTC)):
            raise AuthorizationError("ACCOUNT_DISABLED", 403, "账号已被停用。")
        return row

    async def require_session(self, request: Request):
        if not self.enabled:
            raise AuthorizationError("AUTH_DISABLED", 401, "登录能力当前未启用。")
        user = await self.session(request)
        if user is None:
            raise AuthorizationError("AUTHENTICATION_REQUIRED", 401, "请先登录。")
        return user

    async def require_action(self, request: Request, action: str, project_id: str | None = None):
        from .permissions import CATALOG

        if action not in CATALOG["actions"]:
            raise AuthorizationError("CAPABILITY_DENIED", 403, "未知权限。")
        project_scope = CATALOG["actions"][action]["scope"] == "project"
        if project_scope and not project_id:
            raise AuthorizationError("PROJECT_CONTEXT_REQUIRED", 403, "此操作需要项目上下文。")
        if not self.enabled:
            return ActionContext("local-system-admin", None, "owner" if project_id else None, True)
        user = await self.require_session(request)
        if user["must_change_password"]:
            raise AuthorizationError("PASSWORD_CHANGE_REQUIRED", 428, "请先修改初始密码。")
        if user["role"] == "admin":
            return ActionContext(user["id"], user, "owner" if project_id else None, False)
        project = None
        if project_scope:
            project = await self.database.fetch_one(
                """SELECT CASE WHEN p.owner_id=%s THEN 'owner' ELSE m.role END AS role
                   FROM projects p LEFT JOIN project_memberships m
                   ON m.project_id=p.id AND m.user_id=%s WHERE p.id=%s""",
                (user["id"], user["id"], project_id),
            )
            if not project or not project["role"]:
                raise AuthorizationError("PROJECT_NOT_FOUND", 404, "项目不存在或当前用户无权访问。")
        policy = await self.permission_policy(user)
        decision = evaluate_permission(
            {
                "action": action,
                "actor": {"id": user["id"], "platformRole": "member"},
                "project": {"id": project_id, "role": project["role"]} if project else None,
                "policy": policy,
            }
        )
        if not decision["allowed"]:
            raise AuthorizationError(
                "CAPABILITY_DENIED", 403, "当前用户缺少执行此操作所需的能力权限。"
            )
        return ActionContext(user["id"], user, project["role"] if project else None, False)

    async def permission_policy(self, user):
        try:
            profile = await self.database.fetch_one(
                """SELECT id,key,name FROM permission_profiles
                   WHERE id=%s OR (%s::text IS NULL AND is_default=true)
                   ORDER BY key LIMIT 1""",
                (user["permission_profile_id"], user["permission_profile_id"]),
            )
            if not profile:
                raise ValueError("Missing permission profile")
            grants = await self.database.fetch_all(
                """SELECT permission_key AS "permissionKey",effect
                   FROM permission_profile_grants WHERE profile_id=%s""",
                (profile["id"],),
            )
            overrides = await self.database.fetch_all(
                """SELECT permission_key AS "permissionKey",effect,expires_at AS "expiresAt"
                   FROM user_permission_overrides WHERE user_id=%s""",
                (user["id"],),
            )
        except Exception as error:
            raise AuthorizationError(
                "PERMISSION_POLICY_UNAVAILABLE", 403, "无法确认当前用户的能力权限。"
            ) from error
        return {"profile": {**profile, "grants": grants}, "userOverrides": overrides}

    async def require_project_list(self, request: Request):
        if not self.enabled:
            return None
        user = await self.require_session(request)
        if user["must_change_password"]:
            raise AuthorizationError("PASSWORD_CHANGE_REQUIRED", 428, "请先修改初始密码。")
        if user["role"] == "admin":
            return user
        decision = evaluate_permission(
            {
                "action": "project.read",
                "actor": {"id": user["id"], "platformRole": "member"},
                "project": {"id": "project-list", "role": "viewer"},
                "policy": await self.permission_policy(user),
            }
        )
        if not decision["allowed"]:
            raise AuthorizationError(
                "CAPABILITY_DENIED", 403, "当前用户缺少查看项目列表所需的能力权限。"
            )
        return user


def authorization_from_env(database):
    mode = os.getenv("ALPHADECK_AUTH_MODE", "disabled").strip().lower()
    if mode not in {"disabled", "local"}:
        raise ValueError("Invalid ALPHADECK_AUTH_MODE")
    secret = (os.getenv("ALPHADECK_AUTH_SECRET") or os.getenv("BETTER_AUTH_SECRET", "")).strip()
    if mode == "local" and len(secret) < 32:
        raise ValueError("Local auth requires a secret of at least 32 characters")
    return Authorization(
        database,
        enabled=mode == "local",
        secret=secret,
    )
