from uuid import uuid4

from .allocation import reconcile_projects
from .permissions import CATALOG
from .quota import DEFAULTS


async def ensure_access_control(database):
    async with database.transaction() as tx:
        await tx.execute(
            "SELECT pg_advisory_xact_lock(hashtext('alphadeck:ensure-access-control'))"
        )
        await tx.execute(
            "UPDATE permission_profiles SET is_default=false,updated_at=now() WHERE is_default=true AND key <> 'member-default'"
        )
        profiles = {}
        grants = 0
        for key, name, description in (
            (
                "member-default",
                "标准研究员",
                "普通成员的标准研究能力。项目内的最终权限仍受 owner/editor/viewer 角色约束。",
            ),
            ("readonly-default", "只读研究员", "只能读取已授权项目、源码、量化数据和研究报告。"),
        ):
            row = await (
                await tx.execute(
                    """INSERT INTO permission_profiles(id,key,name,description,is_default,updated_at)
                VALUES (%s,%s,%s,%s,%s,now()) ON CONFLICT(key) DO UPDATE SET name=excluded.name,description=excluded.description,is_default=excluded.is_default,updated_at=now() RETURNING id""",
                    (str(uuid4()), key, name, description, key == "member-default"),
                )
            ).fetchone()
            profiles[key] = row["id"]
            rules = CATALOG["profiles"][key]
            desired = [
                (permission, effect) for effect in ("allow", "deny") for permission in rules[effect]
            ]
            await tx.execute(
                "DELETE FROM permission_profile_grants WHERE profile_id=%s AND NOT(permission_key=ANY(%s))",
                (row["id"], [permission for permission, _ in desired]),
            )
            for permission, effect in desired:
                await tx.execute(
                    """INSERT INTO permission_profile_grants(id,profile_id,permission_key,effect,updated_at)
                    VALUES (%s,%s,%s,%s,now()) ON CONFLICT(profile_id,permission_key) DO UPDATE SET effect=excluded.effect,updated_at=now()""",
                    (str(uuid4()), row["id"], permission, effect),
                )
            grants += len(desired)
        profile = DEFAULTS["profile"]
        await tx.execute(
            "UPDATE quota_profiles SET is_default=false,updated_at=now() WHERE is_default=true AND key<>%s",
            (profile["key"],),
        )
        quota = await (
            await tx.execute(
                """INSERT INTO quota_profiles(id,key,name,description,is_default,updated_at)
            VALUES (%s,%s,%s,%s,true,now()) ON CONFLICT(key) DO UPDATE SET name=excluded.name,description=excluded.description,is_default=true,updated_at=now() RETURNING id""",
                (str(uuid4()), profile["key"], profile["name"], profile["description"]),
            )
        ).fetchone()
        await tx.execute(
            "DELETE FROM quota_rules WHERE profile_id=%s AND NOT(metric=ANY(%s))",
            (quota["id"], [rule["metric"] for rule in DEFAULTS["rules"]]),
        )
        for rule in DEFAULTS["rules"]:
            await tx.execute(
                """INSERT INTO quota_rules(id,profile_id,metric,"limit",enforcement,window_type,window_seconds,reservation_ttl_seconds,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT(profile_id,metric) DO UPDATE SET "limit"=excluded."limit",enforcement=excluded.enforcement,window_type=excluded.window_type,window_seconds=excluded.window_seconds,reservation_ttl_seconds=excluded.reservation_ttl_seconds,updated_at=now()""",
                (
                    str(uuid4()),
                    quota["id"],
                    rule["metric"],
                    int(rule["limit"]),
                    rule["enforcement"],
                    rule["windowType"],
                    rule["windowSeconds"],
                    rule["reservationTtlSeconds"],
                ),
            )
        assigned_permissions = await tx.execute(
            "UPDATE auth_users SET permission_profile_id=%s,updated_at=now() WHERE role='member' AND permission_profile_id IS NULL",
            (profiles["member-default"],),
        )
        assigned_quota = await tx.execute(
            "UPDATE auth_users SET quota_profile_id=%s,updated_at=now() WHERE role='member' AND quota_profile_id IS NULL",
            (quota["id"],),
        )
        administrators = await tx.execute(
            "UPDATE auth_users SET permission_profile_id=NULL,quota_profile_id=NULL,updated_at=now() WHERE role='admin' AND (permission_profile_id IS NOT NULL OR quota_profile_id IS NOT NULL)"
        )
        actors = await (
            await tx.execute(
                "SELECT owner_id AS id FROM projects WHERE owner_id IS NOT NULL UNION SELECT actor_user_id AS id FROM usage_buckets WHERE metric='projects.owned' ORDER BY id"
            )
        ).fetchall()
        reconciled = [
            await reconcile_projects(tx, actor["id"], trigger="ensure-access-control")
            for actor in actors
        ]
        return {
            "permissionProfiles": len(profiles),
            "permissionGrants": grants,
            "quotaProfiles": 1,
            "quotaRules": len(DEFAULTS["rules"]),
            "membersAssignedPermissionProfile": assigned_permissions.rowcount,
            "membersAssignedQuotaProfile": assigned_quota.rowcount,
            "administratorsUnbound": administrators.rowcount,
            "projectAllocations": {
                "actors": len(reconciled),
                "reconciled": sum(row["status"] == "reconciled" for row in reconciled),
                "unchanged": sum(row["status"] == "unchanged" for row in reconciled),
                "skippedActiveReservations": sum(
                    row["status"] == "skipped-active-reservations" for row in reconciled
                ),
                "expiredReservations": sum(row["expiredReservations"] for row in reconciled),
            },
        }
