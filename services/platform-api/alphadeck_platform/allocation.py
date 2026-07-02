from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from .quota import bucket, bucket_lock, expire, one, window


async def reconcile_projects(connection, actor, *, at=None, trigger="unspecified"):
    at = at or datetime.now(UTC)
    selected = window("lifetime", at)
    await bucket_lock(connection, actor, "projects.owned", selected)
    user = await one(connection, "SELECT role FROM auth_users WHERE id=%s", (actor,))
    if not user:
        raise ValueError("Cannot reconcile project allocation for missing actor")
    current = await bucket(connection, actor, "projects.owned", selected)
    previous_used, previous_reserved = current["used"], current["reserved"]
    expired = await one(
        connection,
        "SELECT count(*) AS n FROM quota_reservations WHERE bucket_id=%s AND status='active' AND expires_at<=%s",
        (current["id"], at),
    )
    current = await expire(connection, current["id"], at)
    active = await one(
        connection,
        "SELECT count(*) AS n FROM quota_reservations WHERE bucket_id=%s AND status='active' AND expires_at>%s",
        (current["id"], at),
    )
    actual = (
        await one(connection, "SELECT count(*) AS n FROM projects WHERE owner_id=%s", (actor,))
    )["n"]
    adjustment = 0 if active["n"] else actual - current["used"]
    status = (
        "skipped-active-reservations"
        if active["n"]
        else "reconciled"
        if adjustment
        else "unchanged"
    )
    if not active["n"] and (adjustment or current["reserved"]):
        version = current["version"]
        current = await one(
            connection,
            "UPDATE usage_buckets SET used=%s,reserved=0,version=version+1,updated_at=%s WHERE id=%s RETURNING *",
            (actual, at, current["id"]),
        )
        if adjustment:
            await connection.execute(
                """INSERT INTO usage_events
                (id,idempotency_key,actor_user_id,bucket_id,metric,quantity,source_type,source_id,enforcement_exempt,occurred_at,metadata)
                VALUES (%s,%s,%s,%s,'projects.owned',%s,'allocation_reconciliation',%s,%s,%s,%s)""",
                (
                    str(uuid4()),
                    f"quota-reconcile:projects-owned:{current['id']}:v{version}:to:{actual}",
                    actor,
                    current["id"],
                    adjustment,
                    actor,
                    user["role"] == "admin",
                    at,
                    Jsonb(
                        {
                            "trigger": trigger,
                            "previousUsed": str(previous_used),
                            "authoritativeUsed": str(actual),
                            "expiredReservations": expired["n"],
                        }
                    ),
                ),
            )
    return {
        "actorUserId": actor,
        "status": status,
        "authoritativeUsed": actual,
        "previousUsed": previous_used,
        "currentUsed": current["used"],
        "previousReserved": previous_reserved,
        "currentReserved": current["reserved"],
        "expiredReservations": expired["n"],
        "activeReservations": active["n"],
        "adjustment": adjustment,
    }


async def reconcile_all(database, *, trigger="startup"):
    async with database.transaction() as connection:
        actors = await (
            await connection.execute(
                "SELECT owner_id AS id FROM projects WHERE owner_id IS NOT NULL UNION SELECT actor_user_id AS id FROM usage_buckets WHERE metric='projects.owned' ORDER BY id"
            )
        ).fetchall()
        results = [
            await reconcile_projects(connection, actor["id"], trigger=trigger) for actor in actors
        ]
    return {
        "actors": len(results),
        "reconciled": sum(row["status"] == "reconciled" for row in results),
        "unchanged": sum(row["status"] == "unchanged" for row in results),
        "skippedActiveReservations": sum(
            row["status"] == "skipped-active-reservations" for row in results
        ),
        "expiredReservations": sum(row["expiredReservations"] for row in results),
    }
