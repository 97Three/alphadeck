import asyncio
import os
import re
from urllib.parse import urlsplit
from uuid import uuid4

from psycopg.types.json import Jsonb

from .allocation import reconcile_projects
from .auth import authorization_from_env
from .config import Settings, load_environment
from .credentials import AuthOptions
from .database import Database
from .quota import advisory, one


async def bootstrap(database, authorization, options=None):
    if not authorization.enabled:
        raise ValueError("请先设置 ALPHADECK_AUTH_MODE=local，再初始化管理员。")
    options = options or AuthOptions()
    configured_email = os.getenv("ALPHADECK_AUTH_ADMIN_EMAIL", "").strip().lower()
    configured_password = os.getenv("ALPHADECK_AUTH_ADMIN_PASSWORD", "")
    if bool(configured_email) != bool(configured_password):
        raise ValueError("管理员邮箱与密码必须同时配置。")
    local = urlsplit(options.base_url).hostname in {"localhost", "127.0.0.1", "::1"}
    defaults_allowed = (
        local
        and os.getenv("NODE_ENV", "").strip().lower() != "production"
        and os.getenv("ALPHADECK_DEGRADATION_MODE", "").strip().lower() != "strict"
    )
    use_default = not configured_email and defaults_allowed
    defaults = options.local["bootstrap"]["developmentDefaults"]
    email = configured_email or (defaults["email"] if use_default else "")
    password = configured_password or (defaults["password"] if use_default else "")
    name = os.getenv("ALPHADECK_AUTH_ADMIN_NAME", "").strip() or defaults["name"]
    if not re.fullmatch(r"\S+@\S+\.\S+", email):
        raise ValueError("生产/strict 或非本机环境必须显式配置管理员邮箱与密码。")
    hashed = await options.hash_password(password, development_default=use_default)
    async with database.transaction() as connection:
        await advisory(connection, "auth-bootstrap", email)
        user = await one(
            connection, "SELECT id FROM auth_users WHERE email=%s FOR UPDATE", (email,)
        )
        identity = user["id"] if user else str(uuid4())
        if user:
            await connection.execute(
                """UPDATE auth_users SET name=%s,role='admin',email_verified=true,banned=false,ban_reason=NULL,ban_expires=NULL,
                must_change_password=false,password_changed_at=now(),permission_profile_id=NULL,quota_profile_id=NULL,access_version=access_version+1,updated_at=now() WHERE id=%s""",
                (name, identity),
            )
        else:
            await connection.execute(
                "INSERT INTO auth_users (id,email,name,role,email_verified,banned,must_change_password,password_changed_at,updated_at) VALUES (%s,%s,%s,'admin',true,false,false,now(),now())",
                (identity, email, name),
            )
        await connection.execute(
            """INSERT INTO auth_accounts (id,provider_id,account_id,user_id,password,updated_at) VALUES (%s,'credential',%s,%s,%s,now())
            ON CONFLICT (provider_id,account_id) DO UPDATE SET password=excluded.password,updated_at=now()""",
            (str(uuid4()), identity, identity, hashed),
        )
        await connection.execute("DELETE FROM auth_sessions WHERE user_id=%s", (identity,))
        await connection.execute(
            "DELETE FROM user_permission_overrides WHERE user_id=%s", (identity,)
        )
        await connection.execute("DELETE FROM user_quota_overrides WHERE user_id=%s", (identity,))
        await connection.execute(
            "UPDATE projects SET owner_id=%s,updated_at=now() WHERE owner_id IS NULL", (identity,)
        )
        projects = await (
            await connection.execute("SELECT id FROM projects WHERE owner_id=%s", (identity,))
        ).fetchall()
        for project in projects:
            await connection.execute(
                "INSERT INTO project_memberships (id,project_id,user_id,role,updated_at) VALUES (%s,%s,%s,'owner',now()) ON CONFLICT (project_id,user_id) DO NOTHING",
                (str(uuid4()), project["id"], identity),
            )
        allocation = await reconcile_projects(
            connection, identity, trigger="administrator-bootstrap"
        )
        await connection.execute(
            """INSERT INTO auth_audit_events (id,actor_user_id,event_type,target_type,target_id,outcome,metadata)
            VALUES (%s,%s,'user.bootstrap','user',%s,'success',%s)""",
            (
                str(uuid4()),
                identity,
                identity,
                Jsonb(
                    {
                        "usedDevelopmentDefaults": use_default,
                        "passwordChanged": True,
                        "claimedProjects": len(projects),
                        "projectAllocation": {
                            key: str(allocation[key])
                            if key in {"authoritativeUsed", "currentUsed"}
                            else allocation[key]
                            for key in (
                                "status",
                                "authoritativeUsed",
                                "currentUsed",
                                "activeReservations",
                            )
                        },
                    }
                ),
            ),
        )
    return {
        "userId": identity,
        "email": email,
        "usedDevelopmentDefaults": use_default,
        "allocation": allocation,
    }


def main():
    load_environment()
    database = Database(Settings.from_env().database_url)
    result = asyncio.run(bootstrap(database, authorization_from_env(database)))
    print("管理员已初始化（旧会话已撤销）：" + result["email"])
    print("projects.owned: " + result["allocation"]["status"])


if __name__ == "__main__":
    main()
