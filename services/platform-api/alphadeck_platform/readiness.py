import asyncio
import os
import ssl
import time
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

import httpx

from .integration_config import knowledge_config, memory_config
from .knowledge_http import KnowledgeHttp
from .knowledge_http import compatibility_issues as knowledge_issues
from .memory_http import MemoryHttp
from .memory_service import CHAT_CAPABILITIES
from .memory_service import compatibility_issues as memory_issues
from .service_catalog import degradation_config
from .sessions import iso


async def run_probes(definitions, *, checked_at=None):
    async def run(item):
        name, enabled, required, probe = item
        if not enabled:
            return {
                "name": name,
                "enabled": False,
                "required": required,
                "ok": True,
                "status": "disabled",
                "latencyMs": 0,
            }
        started = time.monotonic()
        try:
            await probe()
            ok = True
        except Exception:
            ok = False
        return {
            "name": name,
            "enabled": True,
            "required": required,
            "ok": ok,
            "status": "ok" if ok else "failed",
            "latencyMs": max(0, int((time.monotonic() - started) * 1000 + 0.5)),
        }

    components = await asyncio.gather(*(run(item) for item in definitions))
    return {
        "ok": all(not item["required"] or item["ok"] for item in components),
        "service": "alphadeck-web",
        "checkedAt": iso(checked_at or datetime.now(UTC)),
        "components": components,
    }


async def probe_redis(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("redis", "rediss") or not parsed.hostname:
        raise ValueError("Invalid Redis URL")
    commands = []
    if parsed.password:
        commands.append(
            ["AUTH", unquote(parsed.username), unquote(parsed.password)]
            if parsed.username
            else ["AUTH", unquote(parsed.password)]
        )
    if parsed.path.strip("/") not in ("", "0"):
        commands.append(["SELECT", parsed.path.strip("/")])
    commands.append(["PING"])
    async with asyncio.timeout(2.5):
        reader, writer = await asyncio.open_connection(
            parsed.hostname,
            parsed.port or (6380 if parsed.scheme == "rediss" else 6379),
            ssl=ssl.create_default_context() if parsed.scheme == "rediss" else None,
        )
        try:
            for command in commands:
                parts = [part.encode() for part in command]
                writer.write(
                    b"*"
                    + str(len(parts)).encode()
                    + b"\r\n"
                    + b"".join(
                        b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n" for part in parts
                    )
                )
            await writer.drain()
            for _ in commands:
                line = await reader.readline()
                if not line.startswith(b"+"):
                    raise ValueError("Redis command failed")
        finally:
            writer.close()
            await writer.wait_closed()


async def web_readiness(database, root, *, env=None, transport=None):
    env = os.environ if env is None else env
    components = degradation_config(env)["components"]

    async def http(url):
        async with httpx.AsyncClient(
            transport=transport, timeout=2.5, follow_redirects=False
        ) as client:
            (await client.get(url)).raise_for_status()

    async def db():
        await database.fetch_one("SELECT 1")

    async def workspace():
        if not os.access(root, os.R_OK | os.W_OK):
            raise ValueError("Workspace unavailable")

    async def memory():
        config = memory_config(env)
        async with (
            httpx.AsyncClient(transport=transport) as client,
            MemoryHttp(config, client=client) as port,
        ):
            if memory_issues(
                await port.discover("alphadeck-readiness"), config, CHAT_CAPABILITIES
            ):
                raise ValueError("Incompatible memory service")
            await port.check_ready("alphadeck-readiness")

    async def knowledge():
        config = knowledge_config(env)
        async with (
            httpx.AsyncClient(transport=transport) as client,
            KnowledgeHttp(config, client=client) as port,
        ):
            if knowledge_issues(await port.discover("alphadeck-readiness"), config):
                raise ValueError("Incompatible knowledge service")
            await port.check_ready("alphadeck-readiness")

    functions = {
        "database": db,
        "workspace": workspace,
        "memory": memory,
        "knowledge": knowledge,
        "marketApi": lambda: http(
            env.get("ALPHADECK_MARKET_API_URL", "http://127.0.0.1:8000").rstrip("/") + "/ready"
        ),
        "modelPort": lambda: http(
            env.get("ALPHADECK_MODELPORT_URL", "http://127.0.0.1:38082").rstrip("/") + "/livez"
        ),
        "observability": lambda: http(
            env.get("LOKI_URL", "http://127.0.0.1:33100").rstrip("/") + "/ready"
        ),
        "redis": lambda: probe_redis(env.get("REDIS_URL", "redis://127.0.0.1:6379")),
    }
    return await run_probes(
        [
            (name, config["enabled"], config["required"], functions[name])
            for name, config in components.items()
        ]
        + [("workspace", True, True, workspace)]
    )
