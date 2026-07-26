from datetime import UTC
from uuid import uuid4

from psycopg.types.json import Jsonb


def iso(value):
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def list_sessions(database, user):
    rows = await database.fetch_all(
        """SELECT id,created_at,updated_at,expires_at,ip_address,user_agent
           FROM auth_sessions WHERE user_id=%s AND expires_at>now()
           ORDER BY updated_at DESC""",
        (user["id"],),
    )
    return [
        {
            "id": row["id"],
            "createdAt": iso(row["created_at"]),
            "updatedAt": iso(row["updated_at"]),
            "expiresAt": iso(row["expires_at"]),
            "ipAddress": row["ip_address"],
            "userAgent": row["user_agent"],
            "isCurrent": row["id"] == user["session_id"],
        }
        for row in rows
    ]


async def revoke_sessions(database, user, *, session_id=None, all_others=False):
    if bool(session_id) == bool(all_others):
        raise ValueError("sessionId 或 allOthers 必须且只能提供一个。")
    async with database.transaction() as connection:
        query = (
            """DELETE FROM auth_sessions WHERE user_id=%s AND id<>%s"""
            if all_others
            else """DELETE FROM auth_sessions WHERE user_id=%s AND id=%s"""
        )
        result = await connection.execute(
            query, (user["id"], user["session_id"] if all_others else session_id)
        )
        await connection.execute(
            """INSERT INTO auth_audit_events
               (id,actor_user_id,event_type,target_type,target_id,outcome,metadata)
               VALUES (%s,%s,'session.revoked','session',%s,'success',%s)""",
            (
                str(uuid4()),
                user["id"],
                "all-others" if all_others else session_id,
                Jsonb({"revokedCount": result.rowcount}),
            ),
        )
    return result.rowcount
