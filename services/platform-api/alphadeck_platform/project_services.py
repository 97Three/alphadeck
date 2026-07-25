"""Persistent external-service connections. Tokens live in separate encrypted storage."""

import json
from uuid import uuid4

from .quota import advisory, one
from .sessions import iso


def record(row):
    try:
        data = json.loads(row["service_data"])
    except (ValueError, TypeError):
        data = {}
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "provider": row["provider"],
        "status": row["status"],
        "serviceData": data,
        "createdAt": iso(row["created_at"]),
        "updatedAt": iso(row["updated_at"]),
        "lastSyncAt": iso(row["last_sync_at"]) if row["last_sync_at"] else None,
    }


class ProjectServices:
    def __init__(self, database):
        self.database = database

    async def list(self, project):
        return [
            record(row)
            for row in await self.database.fetch_all(
                "SELECT * FROM project_service_connections WHERE project_id=%s ORDER BY created_at DESC,id",
                (project,),
            )
        ]

    async def get(self, project, provider):
        row = await self.database.fetch_one(
            "SELECT * FROM project_service_connections WHERE project_id=%s AND provider=%s ORDER BY created_at,id LIMIT 1",
            (project, provider),
        )
        return record(row) if row else None

    async def save(self, project, provider, data, *, patch=False):
        async with self.database.transaction() as tx:
            await advisory(tx, "project-service", project + ":" + provider)
            old = await one(
                tx,
                "SELECT * FROM project_service_connections WHERE project_id=%s AND provider=%s ORDER BY created_at,id LIMIT 1 FOR UPDATE",
                (project, provider),
            )
            if old:
                value = {**record(old)["serviceData"], **data} if patch else data
                row = await one(
                    tx,
                    "UPDATE project_service_connections SET service_data=%s,status=%s,updated_at=clock_timestamp() WHERE id=%s RETURNING *",
                    (
                        json.dumps(value, ensure_ascii=True, allow_nan=False),
                        old["status"] if patch else "connected",
                        old["id"],
                    ),
                )
            else:
                row = await one(
                    tx,
                    "INSERT INTO project_service_connections (id,project_id,provider,status,service_data,updated_at) VALUES (%s,%s,%s,'connected',%s,clock_timestamp()) RETURNING *",
                    (
                        str(uuid4()),
                        project,
                        provider,
                        json.dumps(data, ensure_ascii=True, allow_nan=False),
                    ),
                )
            return record(row)

    async def delete(self, project, identity):
        async with self.database.transaction() as tx:
            return (
                await tx.execute(
                    "DELETE FROM project_service_connections WHERE project_id=%s AND id=%s",
                    (project, identity),
                )
            ).rowcount > 0
