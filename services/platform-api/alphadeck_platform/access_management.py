import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Request

from .admin import positive_integer, private, require_admin
from .api_errors import ApiError
from .credentials import audit
from .permissions import CATALOG
from .quota import DEFAULTS, WINDOWS, one, resolve_policy, utc, window
from .sessions import iso


def summary(profile):
    return {key: profile[key] for key in ("id", "key", "name")} if profile else None


def rule_view(rule):
    return {
        "metric": rule["metric"],
        "limit": str(rule["limit"]) if rule["limit"] is not None else None,
        "enforcement": rule["enforcement"],
        "windowType": rule["window_type"],
        "windowSeconds": rule["window_seconds"],
        "reservationTtlSeconds": rule["reservation_ttl_seconds"],
    }


async def profiles(connection, kind):
    table = "permission_profiles" if kind == "permission" else "quota_profiles"
    records = await (
        await connection.execute(f"SELECT * FROM {table} ORDER BY is_default DESC,name")
    ).fetchall()
    for profile in records:
        if kind == "permission":
            grants = await (
                await connection.execute(
                    'SELECT permission_key AS "permissionKey",effect FROM permission_profile_grants WHERE profile_id=%s ORDER BY permission_key',
                    (profile["id"],),
                )
            ).fetchall()
            profile["grants"] = grants
        else:
            rules = await (
                await connection.execute(
                    "SELECT * FROM quota_rules WHERE profile_id=%s ORDER BY metric",
                    (profile["id"],),
                )
            ).fetchall()
            profile["rules"] = [rule_view(rule) for rule in rules]
    return records


async def catalog(database):
    async with database.transaction() as connection:
        permission = await profiles(connection, "permission")
        quota = await profiles(connection, "quota")

    def view(profile, child):
        return {
            **summary(profile),
            "description": profile["description"],
            "isDefault": profile["is_default"],
            child: profile[child],
        }

    return {
        "version": CATALOG["version"],
        "actions": [{"key": key, **definition} for key, definition in CATALOG["actions"].items()],
        "permissionProfiles": [view(profile, "grants") for profile in permission],
        "quotaProfiles": [view(profile, "rules") for profile in quota],
    }


async def user_details(database, identity):
    now = datetime.now(UTC)
    async with database.transaction() as connection:
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        user = await one(connection, "SELECT * FROM auth_users WHERE id=%s", (identity,))
        if not user:
            return None
        pp = await profiles(connection, "permission")
        qp = await profiles(connection, "quota")
        default_p = next((profile for profile in pp if profile["is_default"]), None)
        default_q = next((profile for profile in qp if profile["is_default"]), None)
        effective_p = next(
            (profile for profile in pp if profile["id"] == user["permission_profile_id"]), default_p
        )
        selected_q = next(
            (profile for profile in qp if profile["id"] == user["quota_profile_id"]), None
        )
        p_overrides = await (
            await connection.execute(
                "SELECT * FROM user_permission_overrides WHERE user_id=%s ORDER BY permission_key",
                (identity,),
            )
        ).fetchall()
        q_overrides = await (
            await connection.execute(
                "SELECT * FROM user_quota_overrides WHERE user_id=%s ORDER BY metric", (identity,)
            )
        ).fetchall()
        observed = await (
            await connection.execute(
                "SELECT DISTINCT metric FROM usage_buckets WHERE actor_user_id=%s", (identity,)
            )
        ).fetchall()
        grants = effective_p["grants"] if effective_p else []

        def effect(records, action, column):
            return next(
                (
                    row["effect"]
                    for row in records
                    if row[column] == action
                    and row["effect"] in {"allow", "deny"}
                    and (not row.get("expires_at") or utc(row["expires_at"]) > now)
                ),
                None,
            )

        permissions = []
        for action, definition in CATALOG["actions"].items():
            override = effect(p_overrides, action, "permission_key")
            profile = effect(grants, action, "permissionKey")
            admin = user["role"] == "admin"
            permissions.append(
                {
                    "action": action,
                    **definition,
                    "allowed": admin
                    or ("deny" not in (override, profile) and "allow" in (override, profile)),
                    "source": "administrator"
                    if admin
                    else "user-override:" + override
                    if override
                    else "profile:" + profile
                    if profile
                    else "not-granted",
                    "projectRoleRequired": definition["scope"] == "project",
                }
            )
        metrics = sorted(
            {rule["metric"] for rule in DEFAULTS["rules"]}
            | {rule["metric"] for rule in (default_q or {}).get("rules", [])}
            | {rule["metric"] for rule in (selected_q or {}).get("rules", [])}
            | {row["metric"] for row in q_overrides + observed}
        )
        quotas = []
        for metric in metrics:
            policy = await resolve_policy(connection, identity, metric, now)
            selected = window(policy["windowType"], now, policy["windowSeconds"])
            bucket = await one(
                connection,
                "SELECT used,reserved FROM usage_buckets WHERE actor_user_id=%s AND metric=%s AND window_start=%s AND window_end=%s",
                (identity, metric, selected["start"], selected["end"]),
            )
            used, reserved = (bucket["used"], bucket["reserved"]) if bucket else (0, 0)
            if metric == "agent.pending":
                actual = await one(
                    connection,
                    """SELECT count(*) AS n FROM user_requests r LEFT JOIN agent_generation_jobs j ON j.request_id=r.id
                    WHERE r.actor_user_id=%s AND r.status IN ('pending','processing','active','running') AND (j.id IS NULL OR j.status IN ('pending','retry_wait'))""",
                    (identity,),
                )
                used, reserved = 0, actual["n"]
            elif metric == "agent.concurrent":
                actual = await one(
                    connection,
                    "SELECT count(*) AS n FROM agent_generation_jobs j JOIN user_requests r ON r.id=j.request_id WHERE j.status='running' AND r.actor_user_id=%s",
                    (identity,),
                )
                used, reserved = 0, actual["n"]
            limit = policy["limit"]
            quotas.append(
                {
                    "metric": metric,
                    "source": policy["source"],
                    "unlimited": policy["unlimited"],
                    "enforcement": policy["enforcement"],
                    "limit": str(limit) if limit is not None else None,
                    "used": str(used),
                    "reserved": str(reserved),
                    "remaining": str(max(0, limit - used - reserved))
                    if limit is not None
                    else None,
                    "exceeded": limit is not None and used + reserved > limit,
                    "windowType": policy["windowType"],
                    "windowStart": iso(selected["start"]),
                    "windowEnd": iso(selected["end"]),
                }
            )
    return {
        "user": {
            **{key: user[key] for key in ("id", "name", "email", "role")},
            "isAdmin": user["role"] == "admin",
            "accessVersion": user["access_version"],
        },
        "permissionProfile": summary(effective_p),
        "quotaProfile": summary(selected_q),
        "permissions": permissions,
        "permissionOverrides": [
            {
                "permissionKey": row["permission_key"],
                "effect": row["effect"],
                "reason": row["reason"],
                "expiresAt": iso(row["expires_at"]) if row["expires_at"] else None,
            }
            for row in p_overrides
            if row["permission_key"] in CATALOG["actions"]
        ],
        "quotas": quotas,
        "quotaOverrides": [
            {
                **rule_view(row),
                "isUnlimited": row["is_unlimited"],
                "reason": row["reason"],
                "expiresAt": iso(row["expires_at"]) if row["expires_at"] else None,
            }
            for row in q_overrides
        ],
    }


def expiration(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid expiresAt")
    return utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))


def validate_update(body):
    if (
        not isinstance(body, dict)
        or body.keys()
        - {
            "expectedAccessVersion",
            "reason",
            "permissionProfileId",
            "quotaProfileId",
            "permissionOverrides",
            "quotaOverrides",
        }
        or type(body.get("expectedAccessVersion")) is not int
        or body["expectedAccessVersion"] < 0
        or not isinstance(body.get("reason"), str)
        or not 3 <= len(body["reason"].strip()) <= 500
    ):
        raise ValueError("Invalid request payload")
    result = {**body, "reason": body["reason"].strip()}
    for key in ("permissionProfileId", "quotaProfileId"):
        if (
            key in result
            and result[key] is not None
            and (not isinstance(result[key], str) or not result[key])
        ):
            raise ValueError("Invalid profile ID")
    for key, field in (("permissionOverrides", "permissionKey"), ("quotaOverrides", "metric")):
        if key not in result:
            continue
        rows = result[key]
        if (
            not isinstance(rows, list)
            or len(rows) > 100
            or any(not isinstance(row, dict) for row in rows)
        ):
            raise ValueError("Invalid overrides")
        seen, normalized = set(), []
        for row in rows:
            name = row.get(field)
            if not isinstance(name, str) or name.strip() in seen:
                raise ValueError("Invalid or duplicate override")
            name = name.strip()
            seen.add(name)
            if field == "permissionKey":
                if (
                    row.keys() - {"permissionKey", "effect", "expiresAt"}
                    or name not in CATALOG["actions"]
                    or row.get("effect") not in {"allow", "deny"}
                ):
                    raise ValueError("Invalid permission override")
                normalized.append(
                    {**row, field: name, "expiresAt": expiration(row.get("expiresAt"))}
                )
                continue
            if row.keys() - {
                "metric",
                "isUnlimited",
                "limit",
                "enforcement",
                "windowType",
                "windowSeconds",
                "reservationTtlSeconds",
                "expiresAt",
            } or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,159}", name):
                raise ValueError("Invalid quota override")
            entry = {
                "isUnlimited": False,
                "enforcement": "observe",
                "windowType": "month",
                "windowSeconds": None,
                "reservationTtlSeconds": 900,
                **row,
                field: name,
                "expiresAt": expiration(row.get("expiresAt")),
            }
            if (
                type(entry["isUnlimited"]) is not bool
                or entry["enforcement"] not in {"observe", "warn", "hard"}
                or entry["windowType"] not in WINDOWS
                or type(entry["reservationTtlSeconds"]) is not int
                or not 1 <= entry["reservationTtlSeconds"] <= 86400
            ):
                raise ValueError("Invalid quota policy")
            limit = entry.get("limit")
            if entry["isUnlimited"]:
                if limit is not None:
                    raise ValueError("Unlimited quota cannot have a limit")
            elif (
                not isinstance(limit, str)
                or not re.fullmatch(r"\d+", limit)
                or not 0 < int(limit) <= 9223372036854775807
            ):
                raise ValueError("Metered quota requires a positive limit")
            seconds = entry["windowSeconds"]
            if entry["windowType"] == "fixed":
                if type(seconds) is not int or not 1 <= seconds <= 31536000:
                    raise ValueError("Invalid fixed window")
            elif seconds is not None:
                raise ValueError("Only fixed windows accept windowSeconds")
            normalized.append(entry)
        result[key] = normalized
    return result


def access_routes(services, credentials):
    router = APIRouter()

    @router.get("/api/admin/access-control")
    async def access_catalog(request: Request):
        database, auth = services()
        await require_admin(auth, request, credentials().options)
        return private({"success": True, "data": await catalog(database)})

    @router.get("/api/account/usage")
    async def usage(request: Request):
        database, auth = services()
        user = await auth.require_session(request)
        return private({"success": True, "data": await user_details(database, user["id"])})

    @router.get("/api/admin/users/{user_id}/access")
    async def get_access(request: Request, user_id: str):
        database, auth = services()
        await require_admin(auth, request, credentials().options)
        details = await user_details(database, user_id)
        if details is None:
            raise ApiError(404, "USER_NOT_FOUND", "用户不存在。")
        return private({"success": True, "data": details})

    @router.patch("/api/admin/users/{user_id}/access")
    async def set_access(request: Request, user_id: str):
        database, auth = services()
        actor = await require_admin(auth, request, credentials().options, fresh=True)
        try:
            body = validate_update(await request.json())
        except (ValueError, TypeError):
            raise ApiError(400, "权限与配额操作失败", "Invalid request payload") from None
        async with database.transaction() as connection:
            target = await one(
                connection, "SELECT role FROM auth_users WHERE id=%s FOR UPDATE", (user_id,)
            )
            if not target:
                raise ApiError(404, "USER_NOT_FOUND", "用户不存在。")
            if target["role"] == "admin" and (
                body.get("permissionProfileId")
                or body.get("quotaProfileId")
                or body.get("permissionOverrides")
                or body.get("quotaOverrides")
            ):
                raise ApiError(
                    409,
                    "ADMIN_POLICY_IS_UNLIMITED",
                    "管理员固定为全部权限和无限配额；可以查看实际用量，但不能设置限制。",
                )
            for key, table, code in (
                ("permissionProfileId", "permission_profiles", "PERMISSION_PROFILE_NOT_FOUND"),
                ("quotaProfileId", "quota_profiles", "QUOTA_PROFILE_NOT_FOUND"),
            ):
                if body.get(key) and not await one(
                    connection, f"SELECT id FROM {table} WHERE id=%s", (body[key],)
                ):
                    raise ApiError(400, code, "权限或配额模板不存在。")
            for rule in body.get("quotaOverrides", []):
                metric = rule["metric"]
                if not await one(
                    connection, "SELECT id FROM quota_rules WHERE metric=%s LIMIT 1", (metric,)
                ):
                    raise ApiError(400, "UNKNOWN_QUOTA_METRIC", "未知配额指标：" + metric)
                builtin = next((row for row in DEFAULTS["rules"] if row["metric"] == metric), None)
                if builtin:
                    if (
                        rule["windowType"] != builtin["windowType"]
                        or rule["windowSeconds"] is not None
                    ):
                        raise ApiError(
                            400,
                            "QUOTA_METRIC_WINDOW_FIXED",
                            f"{metric} 的统计窗口固定为 {builtin['windowType']}，不能通过用户覆盖改变指标语义。",
                        )
                    if rule["reservationTtlSeconds"] != builtin["reservationTtlSeconds"]:
                        raise ApiError(
                            400,
                            "QUOTA_RESERVATION_TTL_FIXED",
                            metric + " 的预留租期由平台固定管理，不能通过用户覆盖改变。",
                        )
                    if (
                        metric in {"projects.owned", "agent.pending", "agent.concurrent"}
                        and rule["enforcement"] != "hard"
                    ):
                        raise ApiError(
                            400,
                            "STRUCTURAL_QUOTA_MUST_BE_HARD",
                            metric + " 是结构性配额，必须保持 hard enforcement。",
                        )
                    if (
                        metric in {"llm.total_tokens.monthly", "quant.data_units.daily"}
                        and rule["enforcement"] == "hard"
                    ):
                        raise ApiError(
                            400,
                            "POSTPAID_QUOTA_CANNOT_BE_HARD",
                            metric + " 当前按实际结果结算，只支持 observe 或 warn。",
                        )
            changed = await connection.execute(
                """UPDATE auth_users SET permission_profile_id=CASE WHEN %s THEN %s ELSE permission_profile_id END,
                quota_profile_id=CASE WHEN %s THEN %s ELSE quota_profile_id END,access_version=access_version+1,updated_at=now()
                WHERE id=%s AND access_version=%s""",
                (
                    "permissionProfileId" in body,
                    body.get("permissionProfileId"),
                    "quotaProfileId" in body,
                    body.get("quotaProfileId"),
                    user_id,
                    body["expectedAccessVersion"],
                ),
            )
            if changed.rowcount != 1:
                raise ApiError(
                    409,
                    "ACCESS_POLICY_VERSION_CONFLICT",
                    "权限配置已被其他管理员更新，请刷新后重试。",
                )
            if "permissionOverrides" in body:
                await connection.execute(
                    "DELETE FROM user_permission_overrides WHERE user_id=%s", (user_id,)
                )
                for row in body["permissionOverrides"]:
                    await connection.execute(
                        "INSERT INTO user_permission_overrides (id,user_id,permission_key,effect,reason,expires_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,now())",
                        (
                            str(uuid4()),
                            user_id,
                            row["permissionKey"],
                            row["effect"],
                            body["reason"],
                            row["expiresAt"],
                        ),
                    )
            if "quotaOverrides" in body:
                await connection.execute(
                    "DELETE FROM user_quota_overrides WHERE user_id=%s", (user_id,)
                )
                for row in body["quotaOverrides"]:
                    await connection.execute(
                        """INSERT INTO user_quota_overrides (id,user_id,metric,is_unlimited,"limit",enforcement,window_type,window_seconds,reservation_ttl_seconds,reason,expires_at,updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                        (
                            str(uuid4()),
                            user_id,
                            row["metric"],
                            row["isUnlimited"],
                            None if row["isUnlimited"] else int(row["limit"]),
                            row["enforcement"],
                            row["windowType"],
                            row["windowSeconds"],
                            row["reservationTtlSeconds"],
                            body["reason"],
                            row["expiresAt"],
                        ),
                    )
        await audit(
            database,
            request,
            "admin.access_policy_updated",
            actor=actor["id"],
            target_type="user",
            target_id=user_id,
            metadata={
                "reason": body["reason"],
                "permissionOverrideCount": len(body.get("permissionOverrides", [])),
                "quotaOverrideCount": len(body.get("quotaOverrides", [])),
                "expectedAccessVersion": body["expectedAccessVersion"],
            },
        )
        return private({"success": True, "data": await user_details(database, user_id)})

    @router.get("/api/admin/audit")
    async def audit_events(request: Request):
        database, auth = services()
        await require_admin(auth, request, credentials().options)
        prefix = request.query_params.get("eventType", "").strip()
        limit = positive_integer(request.query_params.get("limit"), 50, 100)
        rows = await database.fetch_all(
            """SELECT e.*,u.id AS actor_id,u.name AS actor_name,u.email AS actor_email FROM auth_audit_events e
            LEFT JOIN auth_users u ON u.id=e.actor_user_id WHERE starts_with(e.event_type,%s) ORDER BY e.created_at DESC LIMIT %s""",
            (prefix, limit),
        )
        return private(
            {
                "success": True,
                "data": [
                    {
                        "id": row["id"],
                        "eventType": row["event_type"],
                        "targetType": row["target_type"],
                        "targetId": row["target_id"],
                        "outcome": row["outcome"],
                        "ipAddress": row["ip_address"],
                        "metadata": row["metadata"],
                        "createdAt": iso(row["created_at"]),
                        "actor": {
                            "id": row["actor_id"],
                            "name": row["actor_name"],
                            "email": row["actor_email"],
                        }
                        if row["actor_id"]
                        else None,
                    }
                    for row in rows
                ],
            }
        )

    return router
