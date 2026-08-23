import asyncio

import httpx
import pytest

from alphadeck_platform.generated_sandbox import generated_env, wrap_command
from alphadeck_platform.preview_network import (
    market_path,
    market_proxy,
    market_target,
    preview_proxy,
)


def test_generated_process_environment_does_not_inherit_platform_credentials(tmp_path):
    environment = {
        "PATH": "/bin",
        "DEEPSEEK_API_KEY": "private",
        "DATABASE_URL": "private",
        "HTTP_PROXY": "http://proxy",
        "ALPHADECK_ADMIN_TOKEN": "private",
        "NODE_OPTIONS": "--require /private.js",
    }
    assert generated_env(
        tmp_path, {"PORT": "4100", "DATABASE_URL": "injected"}, environment=environment
    ) == {
        "CI": "1",
        "NEXT_TELEMETRY_DISABLED": "1",
        "ALPHADECK_WORKSPACE_ROOT": str(tmp_path),
        "PATH": "/bin",
        "PORT": "4100",
    }
    assert (
        generated_env(tmp_path, install=True, environment=environment)["HTTP_PROXY"]
        == "http://proxy"
    )


def test_generated_sandbox_cannot_be_disabled_without_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHADECK_GENERATED_SANDBOX", "0")
    monkeypatch.delenv("ALPHADECK_ALLOW_UNSANDBOXED_GENERATED_CODE", raising=False)
    with pytest.raises(RuntimeError, match="namespace sandbox"):
        wrap_command(tmp_path, "node", ["page.js"])
    monkeypatch.setenv("ALPHADECK_ALLOW_UNSANDBOXED_GENERATED_CODE", "1")
    assert wrap_command(tmp_path, "node", ["page.js"]) == ["node", "page.js"]


def test_market_bridge_accepts_only_read_only_api_paths():
    for method, path in (
        ("POST", "/api/v1/query"),
        ("GET", "/api/v1/../admin"),
        ("GET", "/api/v1/%2e%2e/admin"),
        ("GET", "/api/v1/%2e%2e%2fadmin"),
        ("GET", "http://other/api/v1/data"),
        ("GET", "/api/private"),
    ):
        assert market_path(method, path) is None
    assert market_path("GET", "/api/v1/quotes?symbol=600519") == "/api/v1/quotes?symbol=600519"
    for target in (
        "https://localhost:8000",
        "http://user:password@localhost:8000",
        "http://localhost:8000/private",
        "http://localhost:8000/?secret=x",
    ):
        with pytest.raises(ValueError):
            market_target(target)


def test_native_market_bridge_strips_credentials_and_write_requests(tmp_path):
    async def run():
        calls = []

        async def upstream(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={"symbol": "600519", "close": 1234},
                headers={"Cache-Control": "max-age=1"},
            )

        path = tmp_path / "market.sock"
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            proxy = await market_proxy(path, client=client, target="http://127.0.0.1:8100")
            try:
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(
                    b"GET /api/v1/quotes?symbol=600519 HTTP/1.1\r\nHost: arbitrary\r\nAuthorization: Bearer private\r\nCookie: private\r\n\r\n"
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 3)
                assert response.startswith(b"HTTP/1.1 200") and b'"symbol":"600519"' in response
                assert (
                    len(calls) == 1
                    and "authorization" not in calls[0].headers
                    and "cookie" not in calls[0].headers
                )
                assert str(calls[0].url) == "http://127.0.0.1:8100/api/v1/quotes?symbol=600519"
                writer.close()
                await writer.wait_closed()
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(b"POST /api/v1/ingestion HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                assert (await asyncio.wait_for(reader.read(), 3)).startswith(b"HTTP/1.1 403")
                writer.close()
                await writer.wait_closed()
                assert len(calls) == 1
            finally:
                await proxy.close()

    asyncio.run(run())


def test_native_preview_proxy_forwards_bidirectional_stream_and_closes_clients(tmp_path):
    async def run():
        async def echo(reader, writer):
            try:
                while chunk := await reader.read(65536):
                    writer.write(chunk)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        path = tmp_path / "preview.sock"
        server = await asyncio.start_unix_server(echo, str(path))
        proxy = await preview_proxy(0, path)
        port = proxy.server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET / HTTP/1.1\r\nUpgrade: websocket\r\n\r\n")
            await writer.drain()
            assert (
                await asyncio.wait_for(reader.read(100), 3)
                == b"GET / HTTP/1.1\r\nUpgrade: websocket\r\n\r\n"
            )
            await proxy.close()
            assert await asyncio.wait_for(reader.read(), 3) == b""
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
