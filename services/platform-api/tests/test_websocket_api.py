import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_projects import project  # noqa: F401

from alphadeck_platform.realtime import Realtime


def test_python_websocket_authorizes_and_receives_cross_process_progress(project):  # noqa: F811
    with TestClient(
        project["app"],
        base_url="http://test",
        cookies={"alphadeck.session_token": project["cookie"]},
    ) as client:
        assert client.get("/api/ws/" + project["id"]).json() == {"ok": True}
        with client.websocket_connect(
            "/api/ws/" + project["id"], headers={"Origin": "http://testserver"}
        ) as socket:
            connected = socket.receive_json()
            assert (
                connected["type"] == "connected" and connected["data"]["transport"] == "websocket"
            )
            socket.send_text("ping")
            assert socket.receive_text() == "pong"
            asyncio.run(
                Realtime(project["database"]).publish(
                    project["id"],
                    {
                        "type": "status",
                        "data": {"status": "controlled-progress", "requestId": "fixture"},
                    },
                )
            )
            assert socket.receive_json() == {
                "type": "status",
                "data": {"status": "controlled-progress", "requestId": "fixture"},
            }
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect(
                "/api/ws/" + project["id"], headers={"Origin": "https://foreign.invalid"}
            ),
        ):
            pass
        assert rejected.value.code == 4403
    with TestClient(project["app"], base_url="http://test") as anonymous:
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            anonymous.websocket_connect("/api/ws/" + project["id"]),
        ):
            pass
        assert rejected.value.code == 4401
