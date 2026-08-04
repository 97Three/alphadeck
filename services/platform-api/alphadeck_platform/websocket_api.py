import asyncio
import re
from contextlib import suppress

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .auth import AuthorizationError
from .frontend_access import same_origin
from .realtime import Realtime, timestamp


def websocket_routes(services):
    router = APIRouter()

    @router.get("/api/ws/{projectId}")
    async def warmup(request: Request, projectId: str):
        _, auth = services()
        await auth.require_action(request, "project.read", projectId)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store, max-age=0"})

    @router.websocket("/api/ws/{projectId}")
    async def connect(socket: WebSocket, projectId: str):
        database, auth = services()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", projectId) or not same_origin(socket):
            await socket.close(code=4403)
            return
        try:
            await auth.require_action(socket, "project.read", projectId)
        except AuthorizationError as error:
            await socket.close(code=4401 if error.status == 401 else 4403)
            return
        async with Realtime(database).listen(projectId) as notifications:
            await socket.accept()
            await socket.send_json(
                {
                    "type": "connected",
                    "data": {
                        "projectId": projectId,
                        "timestamp": timestamp(),
                        "transport": "websocket",
                        "connectionStage": "handshake",
                    },
                }
            )

            async def forward():
                async for event in notifications:
                    await socket.send_json(event)

            task = asyncio.create_task(forward())
            try:
                while True:
                    incoming = asyncio.create_task(socket.receive_text())
                    try:
                        done, _ = await asyncio.wait(
                            (incoming, task), return_when=asyncio.FIRST_COMPLETED
                        )
                        if task in done:
                            await task
                            break
                        value = await incoming
                        if len(value.encode()) > 16384:
                            await socket.close(code=1009)
                            break
                        if value == "ping" or value.replace(" ", "") == '{"type":"ping"}':
                            await socket.send_text("pong")
                    finally:
                        if not incoming.done():
                            incoming.cancel()
                        with suppress(asyncio.CancelledError, WebSocketDisconnect):
                            await incoming
            except WebSocketDisconnect:
                pass
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task

    return router
