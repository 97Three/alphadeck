"""Cross-process progress delivery; PostgreSQL records remain the source of truth."""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from psycopg import sql

from .messages import serialize_message
from .provenance import js_json
from .user_requests import UserRequests

EVENTS = {
    "message",
    "status",
    "project_status",
    "error",
    "connected",
    "heartbeat",
    "preview_error",
    "preview_success",
}


def channel_name(project):
    return "qp_rt_" + hashlib.sha256(project.encode()).hexdigest()[:40]


def timestamp():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sse(event):
    return ("data: " + js_json(event) + "\n\n").encode()


class Realtime:
    def __init__(self, database):
        self.database = database

    async def publish(self, project, event):
        if not isinstance(event, dict) or event.get("type") not in EVENTS:
            raise ValueError("Invalid realtime event")
        payload = js_json(event)
        if len(payload.encode()) >= 7800:
            message = event.get("data")
            if (
                event["type"] != "message"
                or not isinstance(message, dict)
                or not isinstance(message.get("id"), str)
            ):
                raise ValueError("Realtime event exceeds the transport budget")
            row = await self.database.fetch_one(
                "SELECT id FROM messages WHERE id=%s AND project_id=%s", (message["id"], project)
            )
            if not row:
                raise ValueError("Large realtime message requires a durable project-scoped record")
            payload = js_json({"type": "message_ref", "id": row["id"]})
        async with self.database.transaction() as tx:
            await tx.execute("SELECT pg_notify(%s,%s)", (channel_name(project), payload))

    async def decode(self, project, payload):
        try:
            event = json.loads(payload)
            if event.get("type") == "message_ref":
                row = await self.database.fetch_one(
                    "SELECT * FROM messages WHERE id=%s AND project_id=%s", (event["id"], project)
                )
                return {"type": "message", "data": serialize_message(row)} if row else None
            return event if event.get("type") in EVENTS else None
        except (ValueError, TypeError, KeyError, AttributeError):
            return None

    @asynccontextmanager
    async def listen(self, project):
        connection = await psycopg.AsyncConnection.connect(
            self.database.url, autocommit=True, connect_timeout=5
        )
        try:
            await connection.execute(
                sql.SQL("LISTEN {}").format(sql.Identifier(channel_name(project)))
            )

            async def notifications():
                while True:
                    received = False
                    async for notification in connection.notifies(timeout=15, stop_after=1):
                        received = True
                        event = await self.decode(project, notification.payload)
                        if event:
                            yield event
                    if not received:
                        yield {"type": "heartbeat", "data": {"timestamp": timestamp()}}

            yield notifications()
        finally:
            await connection.close()


def chat_runtime_routes(services):
    router = APIRouter()

    @router.get("/api/chat/{project_id}/requests/active")
    async def active(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.read", project_id)
        return await UserRequests(database).active(project_id)

    @router.post("/api/chat/{project_id}/pause")
    async def pause(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "agent.cancel", project_id)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        identity, reason = body.get("requestId"), body.get("reason")
        identity = identity.strip() if isinstance(identity, str) and identity.strip() else None
        reason = (
            reason.strip() if isinstance(reason, str) and reason.strip() else "用户暂停了当前任务"
        )
        cancellation = await UserRequests(database).cancel(project_id, identity, reason)
        await Realtime(database).publish(
            project_id,
            {
                "type": "status",
                "data": {
                    "status": "agent_paused",
                    "message": reason,
                    **({"requestId": identity} if identity else {}),
                    "metadata": cancellation,
                },
            },
        )
        return {"success": True, "data": cancellation}

    @router.get("/api/chat/{project_id}/stream")
    async def stream(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.read", project_id)

        async def events():
            async with Realtime(database).listen(project_id) as notifications:
                yield sse(
                    {
                        "type": "connected",
                        "data": {
                            "projectId": project_id,
                            "timestamp": timestamp(),
                            "transport": "sse",
                        },
                    }
                )
                async for event in notifications:
                    if await request.is_disconnected():
                        break
                    yield sse(event)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
