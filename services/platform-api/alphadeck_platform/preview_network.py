import asyncio
import os
from contextlib import suppress
from http import HTTPStatus
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .config import market_base_url


class NetworkProxy:
    def __init__(self, server, tasks):
        self.server, self.tasks = server, tasks

    async def close(self):
        self.server.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.server.wait_closed()


async def preview_proxy(port, socket_path):
    tasks = set()

    async def pump(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        with suppress(OSError, NotImplementedError):
            writer.write_eof()

    async def accept(reader, writer):
        task, upstream = asyncio.current_task(), None
        tasks.add(task)
        try:
            if len(tasks) > 512:
                return
            async with asyncio.timeout(3):
                other, upstream = await asyncio.open_unix_connection(str(socket_path))
            await asyncio.gather(pump(reader, upstream), pump(other, writer))
        except (OSError, TimeoutError):
            pass
        finally:
            for stream in (writer, upstream):
                if stream:
                    stream.close()
                    with suppress(OSError):
                        await stream.wait_closed()
            tasks.discard(task)

    return NetworkProxy(await asyncio.start_server(accept, "127.0.0.1", port), tasks)


def market_target(configured=None):
    configured = configured or market_base_url()
    parsed = urlsplit(configured)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Generated preview market bridge requires a credential-free internal http:// host:port base URL."
        )
    return parsed.hostname, parsed.port or 80


def market_path(method, target):
    parsed = urlsplit(target)
    decoded = unquote(parsed.path).replace("\\", "/")
    if (
        method not in {"GET", "HEAD"}
        or not decoded.startswith("/api/v1/")
        or any(segment in {".", ".."} for segment in decoded.split("/"))
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
    ):
        return None
    return parsed.path + ("?" + parsed.query if parsed.query else "")


async def market_proxy(socket_path=None, *, client=None, target=None):
    host, port = market_target(target)
    tasks = set()
    owned = client is None
    client = client or httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)

    async def accept(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        status, content, headers, method = (
            502,
            b'{"error":"market data service unavailable"}',
            {},
            "GET",
        )
        try:
            if len(tasks) > 256:
                status, content = 503, b'{"error":"market bridge capacity exceeded"}'
            else:
                async with asyncio.timeout(15):
                    request = await reader.readuntil(b"\r\n\r\n")
                    if len(request) > 32768:
                        raise ValueError("Request headers too large")
                    method, path, version = request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                    if version not in {"HTTP/1.0", "HTTP/1.1"}:
                        raise ValueError("Unsupported HTTP version")
                    path = market_path(method, path)
                    if path is None:
                        status, content = (
                            403,
                            b'{"error":"market bridge permits only GET/HEAD /api/v1/**"}',
                        )
                    else:
                        base = (
                            "http://"
                            + ("[" + host + "]" if ":" in host else host)
                            + ":"
                            + str(port)
                        )
                        async with client.stream(
                            method,
                            base + path,
                            headers={
                                "Accept": "application/json",
                                "User-Agent": "AlphaDeck-Generated-Preview-Market-Bridge/1",
                            },
                        ) as response:
                            status, parts, size = response.status_code, [], 0
                            async for chunk in response.aiter_bytes():
                                size += len(chunk)
                                if size > 32 * 1024 * 1024:
                                    raise ValueError("Market response exceeds its byte budget")
                                parts.append(chunk)
                            content = b"".join(parts)
                            headers = {
                                label: response.headers[key]
                                for key, label in (
                                    ("content-type", "Content-Type"),
                                    ("cache-control", "Cache-Control"),
                                )
                                if key in response.headers
                            }
        except (ValueError, UnicodeError, asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            status, content = 400, b'{"error":"invalid market bridge URL"}'
        except (OSError, TimeoutError, httpx.HTTPError):
            pass
        except asyncio.CancelledError:
            writer.close()
            tasks.discard(task)
            raise
        try:
            phrase = (
                HTTPStatus(status).phrase
                if status in HTTPStatus._value2member_map_
                else "Upstream Response"
            )
            status_line = f"HTTP/1.1 {status} {phrase}\r\n"
            headers = {
                "Content-Type": "application/json",
                **headers,
                "Content-Length": str(len(content)),
                "Connection": "close",
            }
            wire = (
                status_line
                + "".join(key + ": " + value + "\r\n" for key, value in headers.items())
                + "\r\n"
            )
            writer.write(wire.encode("latin-1") + (b"" if method == "HEAD" else content))
            await writer.drain()
        except (OSError, ValueError, UnicodeError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            tasks.discard(task)

    if socket_path is None:
        server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=32769)
    else:
        path = Path(socket_path)
        server = await asyncio.start_unix_server(accept, str(path), limit=32769)
        os.chmod(path, 0o600)
    proxy = NetworkProxy(server, tasks)
    close = proxy.close

    async def shutdown():
        await close()
        if owned:
            await client.aclose()

    proxy.close = shutdown
    return proxy
