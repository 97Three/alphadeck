import asyncio
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

from .config import market_base_url
from .data_quality import assess_response
from .provenance import js_json
from .research_time import constrain_historical_query
from .resource_lock import resource_lock, sync_directory
from .scaffold import controlled_path
from .skill_integrity import read_regular
from .workspace import write_atomic


class MarketDataError(Exception):
    pass


class MarketTransport:
    def __init__(
        self, *, base_url=None, client=None, timeout_seconds=12, max_response_bytes=33554432
    ):
        self.base, self.client = (base_url or market_base_url()).rstrip("/"), client
        if timeout_seconds <= 0 or type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("Market transport limits must be positive")
        self.timeout, self.maximum = timeout_seconds, max_response_bytes

    async def fetch(
        self,
        endpoint,
        *,
        method="GET",
        body=None,
        query=None,
        research_time=None,
        timeout_seconds=None,
        on_quality_warning=None,
    ):
        parsed = urlsplit(endpoint)
        decoded = unquote(parsed.path).replace("\\", "/")
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not decoded.startswith("/api/v1/")
            or any(part in {".", ".."} for part in decoded.split("/"))
            or method not in {"GET", "POST"}
        ):
            raise MarketDataError("Market endpoint must be a relative /api/v1/ path")
        if re.search(r"[\x00-\x1f\x7f]", endpoint):
            raise MarketDataError("Market endpoint contains control characters")
        parameters = {**dict(parse_qsl(parsed.query, keep_blank_values=True)), **(query or {})}
        parameters = constrain_historical_query(parsed.path, parameters, research_time)
        owned = self.client is None
        client = self.client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        try:
            async with asyncio.timeout(timeout_seconds or self.timeout):
                async with client.stream(
                    method,
                    self.base + parsed.path,
                    params=parameters,
                    json=body,
                    timeout=timeout_seconds or self.timeout,
                ) as response:
                    parts, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.maximum:
                            raise MarketDataError("Market response exceeds its byte budget")
                        parts.append(chunk)
                    text = b"".join(parts).decode("utf-8", "replace")
                    if not response.is_success:
                        raise MarketDataError(
                            f"{endpoint} 返回 HTTP {response.status_code}: {text[:500]}"
                        )
                    try:
                        payload = json.loads(
                            text,
                            parse_constant=lambda _: (_ for _ in ()).throw(
                                ValueError("Invalid JSON constant")
                            ),
                        )
                    except ValueError as cause:
                        raise MarketDataError(f"{endpoint} 未返回有效 JSON。") from cause
                    if not isinstance(payload, dict):
                        raise MarketDataError(f"{endpoint} 未返回 JSON 对象。")
                    assessment = assess_response(parsed.path, payload, parameters)
                    if not assessment["usable"]:
                        raise MarketDataError(
                            "行情响应未通过数据检查："
                            + "、".join(
                                item["code"] + "@" + item["path"] for item in assessment["issues"]
                            )
                        )
                    if assessment["status"] == "warning" and on_quality_warning:
                        on_quality_warning(assessment)
                    return payload
        finally:
            if owned:
                await client.aclose()


class WorkspaceArtifacts:
    def __init__(self, root, authority):
        self.root, self.authority = Path(root).resolve(strict=True), authority
        if not callable(authority):
            raise ValueError("Platform artifact writes require an authoritative generation lease")

    async def write(self, relative, value):
        if (
            not isinstance(relative, str)
            or not relative.endswith(".json")
            or relative.split("/", 1)[0] not in {".data-agent", "data_file", "evidence", "public"}
        ):
            raise ValueError("Unsupported platform JSON artifact path")
        content = (js_json(value, indent=2) + "\n").encode()
        if len(content) > 33554432:
            raise ValueError("Platform JSON artifact exceeds its byte budget")
        async with resource_lock(
            self.root, {"purpose": "data_prefetch"}, recover_dead_local_owner=True
        ):

            async def write(_tx):
                controlled_path(self.root, relative)
                path = write_atomic(self.root, relative, content)
                sync_directory(path.parent)
                return relative

            return await self.authority(write)

    async def screenshot(self, relative, content):
        if (
            not re.fullmatch(r"tmp/visual-checks/[A-Za-z0-9_-]{1,160}\.png", relative)
            or not isinstance(content, bytes)
            or not content.startswith(b"\x89PNG\r\n\x1a\n")
            or len(content) > 33554432
        ):
            raise ValueError("Invalid visual validation screenshot")
        async with resource_lock(
            self.root, {"purpose": "visual_evidence"}, recover_dead_local_owner=True
        ):

            async def write(_tx):
                controlled_path(self.root, relative)
                target = write_atomic(self.root, relative, content)
                sync_directory(target.parent)
                return relative

            return await self.authority(write)

    def read(self, relative):
        try:
            payload = json.loads(read_regular(controlled_path(self.root, relative), 33554432))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            return None

    async def mutate(self, operation):
        async with resource_lock(
            self.root, {"purpose": "platform_preparation"}, recover_dead_local_owner=True
        ):
            return await self.authority(operation)

    async def remove_repair_plan(self):
        async def remove(_tx):
            target = controlled_path(self.root, ".data-agent/validation-repair-plan.json")
            target.unlink(missing_ok=True)
            if target.parent.exists():
                sync_directory(target.parent)

        await self.mutate(remove)

    async def event(self, value):
        value = {
            **value,
            "created_at": value.get("created_at")
            or datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        data = (js_json(value) + "\n").encode()
        if len(data) > 262144:
            raise ValueError("Workspace event exceeds its byte budget")
        async with resource_lock(
            self.root, {"purpose": "workspace_event"}, recover_dead_local_owner=True
        ):

            async def append(_tx):
                target = controlled_path(self.root, ".data-agent/events.jsonl")
                target.parent.mkdir(parents=True, exist_ok=True)
                with os.fdopen(
                    os.open(
                        target,
                        os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    ),
                    "ab",
                ) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                sync_directory(target.parent)

            await self.authority(append)


async def collect_datasets(
    symbols, fetch_dataset, *, quotes=None, on_progress=None, assert_active=None
):
    quotes, results = quotes or {}, [None] * len(symbols)
    next_index, completed, succeeded = 0, 0, 0
    reporting = asyncio.Lock()

    async def active():
        if assert_active:
            await assert_active()

    async def worker():
        nonlocal next_index, completed, succeeded
        while next_index < len(symbols):
            index = next_index
            next_index += 1
            await active()
            symbol = symbols[index]
            result = {"asset": None, "rawFiles": [], "warnings": []}
            try:
                result["asset"] = await fetch_dataset(
                    symbol, quotes.get(symbol), result["rawFiles"], result["warnings"]
                )
                succeeded += 1
            except Exception as cause:
                await active()
                result["warnings"].append(f"{symbol} 预取失败：{cause}")
            results[index] = result
            completed += 1
            progress = {
                "completed": completed,
                "total": len(symbols),
                "succeeded": succeeded,
                "failed": completed - succeeded,
            }
            async with reporting:
                if on_progress:
                    try:
                        await on_progress(progress)
                    except Exception:
                        logging.warning("Unable to publish data prefetch progress", exc_info=True)

    tasks = [asyncio.create_task(worker()) for _ in range(min(2, len(symbols)))]
    try:
        settled = await asyncio.gather(*tasks, return_exceptions=True)
        failure = next((value for value in settled if isinstance(value, BaseException)), None)
        if failure:
            raise failure
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "assets": [item["asset"] for item in results if item and item["asset"] is not None],
        "rawFiles": [value for item in results if item for value in item["rawFiles"]],
        "warnings": [value for item in results if item for value in item["warnings"]],
    }
