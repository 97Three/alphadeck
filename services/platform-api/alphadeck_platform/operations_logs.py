import asyncio
import os
import re
import time
from datetime import UTC, datetime

import httpx

from .config import ROOT
from .evaluation_queue import date
from .generation_state import now
from .scaffold import controlled_path
from .service_catalog import degradation_config, raw_endpoint
from .sessions import iso
from .skill_source import strict_json


def parsed_time(value):
    try:
        parsed = date(value) if isinstance(value, str) else None
        return iso(parsed) if parsed else None
    except (ValueError, OverflowError):
        return None


def parse_log_entry(raw, line_number, fallback=None):
    trimmed, timestamp, level, message = raw.strip(), None, None, raw.strip() or raw
    try:
        value = strict_json(trimmed)
        if isinstance(value, dict):
            timestamp = next(
                (
                    result
                    for key in ("timestamp", "time", "created_at")
                    if (result := parsed_time(value.get(key)))
                ),
                None,
            )
            level = (
                value.get("level")
                if isinstance(value.get("level"), str)
                else value.get("severity")
                if isinstance(value.get("severity"), str)
                else None
            )
            message = (
                value.get("message")
                if isinstance(value.get("message"), str)
                else value.get("msg")
                if isinstance(value.get("msg"), str)
                else trimmed
            )
    except ValueError:
        match = re.search(
            r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b", trimmed
        )
        bracket = re.search(r"\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]", trimmed)
        timestamp = parsed_time(match[0]) if match else None
        timestamp = timestamp or (parsed_time(bracket[1]) if bracket else None)
        matched = re.search(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|LOG)\b", trimmed, re.I)
        level = matched[1].upper() if matched else None
    return {
        "id": str(line_number) + "-" + raw.encode()[:12].hex(),
        "lineNumber": line_number,
        "timestamp": timestamp or fallback,
        "timestampSource": "line" if timestamp else "source-modified" if fallback else None,
        "level": level,
        "message": message,
        "raw": raw,
    }


async def probe_url(url, *, timeout=2.5, client=None):
    started, owned = time.monotonic(), client is None
    client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
    try:
        async with asyncio.timeout(timeout):
            response = await client.get(url, timeout=timeout)
        return {
            "ok": response.is_success,
            "status": response.status_code,
            "ms": round((time.monotonic() - started) * 1000),
            "error": None if response.is_success else f"HTTP {response.status_code}",
        }
    except (httpx.HTTPError, TimeoutError) as error:
        return {
            "ok": False,
            "status": None,
            "ms": round((time.monotonic() - started) * 1000),
            "error": str(error) or "Request timed out",
        }
    finally:
        if owned:
            await client.aclose()


def file_log(identity, label, path, *, root=ROOT, entries=False):
    value = {
        "id": identity,
        "label": label,
        "path": path.relative_to(root).as_posix() if path else "<未发现日志文件>",
        "type": "file",
        "exists": False,
        "sizeBytes": 0,
        "modifiedAt": None,
        "lineCount": 0,
        "lines": [],
        "entries": [],
    }
    try:
        if path is None:
            raise ValueError("没有匹配的日志文件")
        path = controlled_path(root, path.relative_to(root).as_posix())
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
            stat = os.fstat(stream.fileno())
            value.update(
                exists=True,
                sizeBytes=stat.st_size,
                modifiedAt=iso(datetime.fromtimestamp(stat.st_mtime, UTC)),
            )
            if entries:
                stream.seek(max(0, stat.st_size - 220000))
                lines = re.split(r"\r?\n", stream.read(220000).decode("utf-8", "replace"))
                if lines and not lines[-1]:
                    lines.pop()
                visible = lines[-180:]
                value.update(
                    lineCount=len(lines),
                    lines=visible,
                    entries=[
                        parse_log_entry(
                            line, max(1, len(lines) - len(visible) + index + 1), value["modifiedAt"]
                        )
                        for index, line in enumerate(visible)
                    ],
                )
    except (OSError, ValueError) as error:
        value["error"] = str(error)
    return value


async def loki_log(*, entries=False, client=None):
    base = (raw_endpoint("loki") or "http://127.0.0.1:33100").rstrip("/")
    query = os.getenv("LOKI_QUERY", '{app="alphadeck"}').replace('\\"', '"')
    grafana = (raw_endpoint("grafana") or "http://127.0.0.1:33012").rstrip("/")
    value = {
        "id": "loki",
        "label": "Loki 集中日志",
        "path": base + " · " + query,
        "type": "loki",
        "exists": False,
        "sizeBytes": 0,
        "modifiedAt": None,
        "lineCount": 0,
        "lines": [],
        "entries": [],
        "query": query,
        "externalUrl": grafana + "/explore",
    }
    if not degradation_config()["components"]["observability"]["enabled"]:
        return {
            **value,
            "path": "配置停用 · " + query,
            "error": "Loki 已按降级配置停用，本地文件日志仍可作为兜底。",
        }
    if not entries:
        probe = await probe_url(base + "/ready", timeout=0.6, client=client)
        return {
            **value,
            "exists": probe["ok"],
            "modifiedAt": now() if probe["ok"] else None,
            **({"error": probe["error"]} if not probe["ok"] else {}),
        }
    owned, client = (
        client is None,
        client or httpx.AsyncClient(trust_env=False, follow_redirects=False),
    )
    try:
        end = int(time.time() * 1000) * 1000000
        response = await client.get(
            base + "/loki/api/v1/query_range",
            params={
                "query": query,
                "start": str(end - 86400000000000),
                "end": str(end),
                "limit": "220",
                "direction": "BACKWARD",
            },
            timeout=1.8,
        )
        if len(response.content) > 2097152:
            raise ValueError("Loki response exceeds size limit")
        body = strict_json(response.content)
        if not response.is_success or not isinstance(body, dict) or body.get("status") != "success":
            return {
                **value,
                "error": body.get("error", body.get("message", f"HTTP {response.status_code}"))
                if isinstance(body, dict)
                else f"HTTP {response.status_code}",
            }
        parsed = []
        for stream in body.get("data", {}).get("result", []):
            labels = " ".join(
                key + "=" + text
                for key, text in stream.get("stream", {}).items()
                if key in ("container", "compose_service", "job", "source")
            )
            for timestamp, line in stream.get("values", []):
                fallback = iso(datetime.fromtimestamp(int(timestamp) // 1000000 / 1000, UTC))
                entry = parse_log_entry(line, 0, fallback)
                parsed.append(
                    {
                        **entry,
                        "id": "loki-" + timestamp + "-" + line.encode()[:8].hex(),
                        "timestampSource": "line" if entry["timestampSource"] == "line" else "loki",
                        "raw": "[" + labels + "] " + line if labels else line,
                    }
                )
        parsed.sort(key=lambda entry: entry["timestamp"] or "")
        parsed = [{**entry, "lineNumber": index + 1} for index, entry in enumerate(parsed)]
        return {
            **value,
            "exists": True,
            "sizeBytes": sum(len(entry["raw"].encode()) for entry in parsed),
            "modifiedAt": parsed[-1]["timestamp"] if parsed else None,
            "lineCount": len(parsed),
            "lines": [entry["raw"] for entry in parsed],
            "entries": parsed,
        }
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        return {**value, "error": str(error)}
    finally:
        if owned:
            await client.aclose()


async def collect_logs(*, root=ROOT, entries=False, client=None):
    directory = controlled_path(root, "tmp/alphadeck-eval-queue/logs")
    candidates = (
        [path for path in directory.glob("*.log") if path.is_file() and not path.is_symlink()]
        if directory.is_dir()
        else []
    )
    latest = max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None
    sources = [
        ("vite-dev", "前端 Vue/Vite dev", root / "data/logs/vite-development.log"),
        ("frontend-runtime", "前端启动脚本", root / "tmp/runtime/frontend.log"),
        ("platform-api", "Python 平台后端", root / "tmp/runtime/platform-api.log"),
        ("market-api", "量化数据后端", root / "tmp/runtime/market-api.log"),
        ("eval-queue", "评测队列最新运行", latest),
    ]
    return [
        await loki_log(entries=entries, client=client),
        *(
            file_log(identity, label, path, root=root, entries=entries)
            for identity, label, path in sources
        ),
    ]
