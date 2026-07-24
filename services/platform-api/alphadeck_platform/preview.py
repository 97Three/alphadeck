import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .artifact_policy import check_artifact_policy
from .generated_sandbox import generated_env, namespace_enabled, wrap_command
from .preview_network import market_proxy, preview_proxy
from .projects import ProjectNotFound
from .resource_lock import resource_lock, sync_directory
from .scaffold import controlled_path, ensure_scaffold
from .skill_integrity import read_regular
from .workspace import write_atomic

RECORD = ".data-agent/preview-runtime.json"


def bounds():
    try:
        start, end = (
            int(os.getenv("PREVIEW_PORT_START", "4100")),
            int(os.getenv("PREVIEW_PORT_END", "4999")),
        )
    except ValueError as cause:
        raise ValueError("Preview port bounds must be integers") from cause
    if not 1 <= start <= end <= 65535:
        raise ValueError("Invalid preview port range")
    return start, end


def persisted_target(url, port):
    try:
        parsed = urlsplit(url or "")
        start, end = bounds()
        if (
            type(port) is not int
            or not start <= port <= end
            or parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.port != port
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        return {"url": url, "port": port}
    except ValueError:
        return None


def read_record(root):
    try:
        value = json.loads(read_regular(controlled_path(root, RECORD), 262144))
        return value if isinstance(value, dict) and value.get("schemaVersion") == 1 else None
    except (FileNotFoundError, ValueError):
        return None


def write_record(root, record):
    controlled_path(root, RECORD)
    path = write_atomic(
        root, RECORD, (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    )
    sync_directory(path.parent)


def owner_start(pid):
    try:
        # /proc stat comm can contain spaces and parentheses; fields after its final ')' are stable.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def owner_alive(record):
    if record.get("hostname") != socket.gethostname() or type(record.get("ownerPid")) is not int:
        return False
    try:
        os.kill(record["ownerPid"], 0)
        if record.get("ownerStart") != owner_start(record["ownerPid"]):
            return False
        child = record.get("childPid")
        if type(child) is not int or child <= 0:
            return False
        os.kill(child, 0)
        return record.get("childStart") == owner_start(child)
    except (OSError, ValueError):
        return False


@dataclass
class Preview:
    project: str
    root: Path
    nonce: str
    port: int
    url: str
    status: str = "starting"
    child: object = None
    logs: object = field(default_factory=lambda: deque(maxlen=400))
    proxies: list = field(default_factory=list)
    tasks: list = field(default_factory=list)
    runtime: object = None
    closing: bool = False

    def info(self):
        return {
            "port": self.port,
            "url": self.url,
            "status": self.status,
            "logs": list(self.logs),
            **({"pid": self.child.pid} if self.child else {}),
        }


class PreviewManager:
    def __init__(
        self,
        projects,
        *,
        readiness_seconds=90,
        command_factory=None,
        prepare=ensure_scaffold,
        policy=check_artifact_policy,
    ):
        self.projects, self.database = projects, projects.database
        self.timeout, self.command_factory, self.prepare, self.policy = (
            readiness_seconds,
            command_factory,
            prepare,
            policy,
        )
        self.processes, self.starts, self.locks = {}, {}, {}

    def status(self, project):
        preview = self.processes.get(project)
        return (
            preview.info()
            if preview
            else {"port": None, "url": None, "status": "stopped", "logs": []}
        )

    async def probe(self, url):
        try:
            async with (
                httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=1.5) as client,
                client.stream("GET", url) as response,
            ):
                return response.is_success
        except httpx.HTTPError:
            return False

    async def reconciled(self, project, persisted_url=None, persisted_port=None):
        local = self.status(project)
        if local["status"] in {"running", "starting"}:
            return local
        target = persisted_target(persisted_url, persisted_port)
        if target and await self.probe(target["url"]):
            return {
                "port": target["port"],
                "url": target["url"],
                "status": "running",
                "logs": local["logs"],
            }
        return local

    async def start(self, project):
        if project in self.starts:
            return await asyncio.shield(self.starts[project])
        task = asyncio.create_task(self._start(project))
        self.starts[project] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self.starts.get(project) is task:
                del self.starts[project]

    async def _start(self, project):
        row = await self.projects.get(project)
        root = self.projects.managed_path(row, must_exist=True)
        async with self.locks.setdefault(project, asyncio.Lock()):
            existing = self.processes.get(project)
            if (
                existing
                and existing.status == "running"
                and existing.child
                and existing.child.returncode is None
                and await self.probe(existing.url)
            ):
                return existing.info()
            if existing:
                await self._terminate(existing)
            preview = None
            try:
                async with resource_lock(
                    root,
                    {"purpose": "preview_start", "projectId": project},
                    recover_dead_local_owner=True,
                ):
                    previous = read_record(root)
                    if (
                        previous
                        and previous.get("status") in {"starting", "running"}
                        and owner_alive(previous)
                    ):
                        target = persisted_target(previous.get("url"), previous.get("port"))
                        if target:
                            ready = await self.probe(target["url"])
                            return {
                                "port": target["port"],
                                "url": target["url"],
                                "status": "running" if ready else "starting",
                                "logs": previous.get("logs", []),
                            }
                    self.prepare(root, project)
                    policy = self.policy(root)
                    if policy["status"] == "failed":
                        raise RuntimeError(
                            "Refusing to execute generated preview before artifact policy passes: "
                            + policy.get("details", policy["summary"])
                        )
                    if not (root / "node_modules").is_dir():
                        await self._install(root)
                    start, end = bounds()
                    for port in range(start, end + 1):
                        candidate = Preview(
                            project, root, uuid4().hex, port, f"http://localhost:{port}"
                        )
                        if namespace_enabled():
                            try:
                                candidate.runtime = (
                                    Path(tempfile.gettempdir())
                                    / "qp-preview"
                                    / hashlib.sha256(
                                        (
                                            str(root) + "\0" + str(port) + "\0" + candidate.nonce
                                        ).encode()
                                    ).hexdigest()[:20]
                                )
                                candidate.runtime.mkdir(parents=True, exist_ok=False, mode=0o700)
                                candidate.proxies.append(
                                    await preview_proxy(port, candidate.runtime / "p.sock")
                                )
                            except OSError:
                                await self._terminate(candidate)
                                continue
                        else:
                            with socket.socket() as probe:
                                try:
                                    probe.bind(("127.0.0.1", port))
                                except OSError:
                                    continue
                        preview = candidate
                        break
                    if not preview:
                        raise RuntimeError("No available preview port in the configured range")
                    overrides = {
                        "PORT": str(preview.port),
                        "WEB_PORT": str(preview.port),
                        "NEXT_PUBLIC_APP_URL": preview.url,
                    }
                    if preview.runtime:
                        preview.proxies.append(await market_proxy(preview.runtime / "m.sock"))
                        overrides.update(
                            ALPHADECK_SANDBOX_PREVIEW_SOCKET=str(preview.runtime / "p.sock"),
                            ALPHADECK_SANDBOX_PREVIEW_PORT=str(preview.port),
                            ALPHADECK_SANDBOX_MARKET_SOCKET=str(preview.runtime / "m.sock"),
                            ALPHADECK_SANDBOX_MARKET_PORT="8000",
                        )
                    else:
                        bridge = await market_proxy()
                        preview.proxies.append(bridge)
                        overrides["ALPHADECK_SANDBOX_MARKET_PORT"] = str(
                            bridge.server.sockets[0].getsockname()[1]
                        )
                    command = (
                        self.command_factory(root, preview.port)
                        if self.command_factory
                        else wrap_command(
                            root, "npm", ["run", "dev", "--", "--port", str(preview.port)]
                        )
                    )
                    preview.child = await asyncio.create_subprocess_exec(
                        *command,
                        cwd=root,
                        env=generated_env(root, overrides),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=sys.platform != "win32",
                    )
                    self.processes[project] = preview
                    self._persist(preview)
                    await self.projects.update(
                        project, {"previewUrl": preview.url, "previewPort": preview.port}
                    )
                    preview.tasks.append(asyncio.create_task(self._logs(preview)))
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    await self._active(preview)
                    if await self.probe(preview.url):
                        async with resource_lock(
                            root,
                            {"purpose": "preview_ready", "projectId": project},
                            recover_dead_local_owner=True,
                        ):
                            await self._active(preview)
                            preview.status = "running"
                            self._persist(preview)
                            await self.projects.update(
                                project,
                                {
                                    "previewUrl": preview.url,
                                    "previewPort": preview.port,
                                    "status": "running",
                                },
                            )
                        preview.tasks.append(asyncio.create_task(self._watch(preview)))
                        return preview.info()
                    await asyncio.sleep(0.2)
                raise RuntimeError("Preview did not become HTTP-ready before its startup deadline")
            except BaseException:
                if preview:
                    preview.status = "error"
                    await self._terminate(preview)
                    async with resource_lock(
                        root,
                        {"purpose": "preview_failed", "projectId": project},
                        recover_dead_local_owner=True,
                    ):
                        current = read_record(root)
                        if current and current.get("nonce") == preview.nonce:
                            self._persist(preview)
                            await self._clear_projection(preview)
                raise

    async def _active(self, preview):
        current = read_record(preview.root)
        if (
            preview.closing
            or not current
            or current.get("nonce") != preview.nonce
            or current.get("status") not in {"starting", "running"}
        ):
            raise RuntimeError("Preview start was cancelled or superseded")
        if preview.child.returncode is not None:
            raise RuntimeError(
                "Preview process exited before readiness: " + "\n".join(preview.logs)[-4000:]
            )
        row = await self.projects.get(preview.project)
        if self.projects.managed_path(row) != preview.root:
            raise RuntimeError("Preview workspace binding changed")

    def _persist(self, preview):
        write_record(
            preview.root,
            {
                "schemaVersion": 1,
                "nonce": preview.nonce,
                "projectId": preview.project,
                "port": preview.port,
                "url": preview.url,
                "status": preview.status,
                "ownerPid": os.getpid(),
                "ownerStart": owner_start(os.getpid()),
                "hostname": socket.gethostname(),
                "childPid": preview.child.pid if preview.child else None,
                "childStart": owner_start(preview.child.pid) if preview.child else None,
                "logs": [line[-300:] for line in list(preview.logs)[-100:]],
            },
        )

    async def _logs(self, preview):
        while chunk := await preview.child.stdout.read(4096):
            for line in chunk.decode("utf-8", "replace").splitlines():
                preview.logs.append(line[-2000:])

    async def _watch(self, preview):
        try:
            while not preview.closing:
                await asyncio.sleep(0.5)
                await self._active(preview)
        except (OSError, ValueError, RuntimeError, ProjectNotFound):
            await self._terminate(preview)
            # A replaced physical owner must never clear the newer owner's database projection.
            with suppress(OSError, ValueError):
                async with resource_lock(
                    preview.root,
                    {"purpose": "preview_exit", "projectId": preview.project},
                    recover_dead_local_owner=True,
                ):
                    current = read_record(preview.root)
                    if current and current.get("nonce") == preview.nonce:
                        preview.status = (
                            "error" if current.get("status") != "stopped" else "stopped"
                        )
                        self._persist(preview)
                        await self._clear_projection(preview)

    async def _clear_projection(self, preview):
        async with self.database.transaction() as tx:
            await tx.execute(
                "UPDATE projects SET preview_url=NULL,preview_port=NULL,status='idle',updated_at=clock_timestamp() WHERE id=%s AND preview_url=%s AND preview_port=%s",
                (preview.project, preview.url, preview.port),
            )

    async def _terminate(self, preview):
        if preview.closing:
            return
        preview.closing = True
        if preview.child and preview.child.returncode is None:
            with suppress(ProcessLookupError):
                if sys.platform == "win32":
                    preview.child.terminate()
                else:
                    os.killpg(preview.child.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(preview.child.wait(), 2)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    if sys.platform == "win32":
                        preview.child.kill()
                    else:
                        os.killpg(preview.child.pid, signal.SIGKILL)
                await preview.child.wait()
        for proxy in preview.proxies:
            await proxy.close()
        for task in preview.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(
            *(task for task in preview.tasks if task is not asyncio.current_task()),
            return_exceptions=True,
        )
        if preview.runtime:
            target = preview.runtime.resolve()
            allowed = Path(tempfile.gettempdir()).resolve() / "qp-preview"
            if target.parent != allowed or not re_full_hash(target.name):
                raise ValueError("Refusing to remove an unexpected preview runtime directory")
            shutil.rmtree(target)

    async def stop(self, project):
        row = await self.projects.get(project)
        root = self.projects.managed_path(row, must_exist=True)
        async with resource_lock(
            root, {"purpose": "preview_stop", "projectId": project}, recover_dead_local_owner=True
        ):
            record = read_record(root)
            if record:
                record["status"] = "stopped"
                write_record(root, record)
            await self.projects.update(
                project, {"previewUrl": None, "previewPort": None, "status": "idle"}
            )
        local = self.processes.get(project)
        if local:
            await self._terminate(local)
            local.status = "stopped"
        return {
            "port": None,
            "url": None,
            "status": "stopped",
            "logs": list(local.logs) if local else (record or {}).get("logs", []),
        }

    async def _install(self, root):
        package = json.loads(read_regular(controlled_path(root, "package.json"), 1048576))
        selected = (package.get("packageManager") or "").split("@", 1)[0]
        if selected not in {"npm", "pnpm", "yarn", "bun"}:
            selected = next(
                (
                    manager
                    for manager, lock in (
                        ("pnpm", "pnpm-lock.yaml"),
                        ("yarn", "yarn.lock"),
                        ("bun", "bun.lockb"),
                    )
                    if (root / lock).is_file()
                ),
                "npm",
            )
        if not shutil.which(selected):
            selected = "npm"
        arguments = [
            selected,
            "install",
            "--ignore-scripts",
            *(["--no-audit", "--no-fund"] if selected == "npm" else []),
        ]
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=root,
            env=generated_env(root, install=True),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
        )
        logs, size = deque(maxlen=400), 0
        try:
            async with asyncio.timeout(300):
                while chunk := await process.stdout.read(4096):
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise RuntimeError("Dependency installer exceeded its output budget")
                    logs.extend(
                        line[-2000:] for line in chunk.decode("utf-8", "replace").splitlines()
                    )
                await process.wait()
            if process.returncode:
                raise RuntimeError("Dependency installation failed: " + "\n".join(logs)[-4000:])
            return list(logs)
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    if sys.platform == "win32":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    async def install_dependencies(self, project):
        row = await self.projects.get(project)
        root = self.projects.managed_path(row, must_exist=True)
        async with resource_lock(
            root,
            {"purpose": "dependency_install", "projectId": project},
            wait_seconds=300,
            recover_dead_local_owner=True,
        ):
            self.prepare(root, project)
            logs = (
                ["Dependencies already installed. Skipped install command."]
                if (root / "node_modules").is_dir()
                else await self._install(root)
            )
            return {"logs": logs}

    async def cleanup_deleted(self):
        removed = []
        for project, preview in list(self.processes.items()):
            try:
                await self.projects.get(project)
            except ProjectNotFound:
                await self._terminate(preview)
                self.processes.pop(project, None)
                removed.append(project)
        return removed

    async def cleanup_all(self):
        projects = list(self.processes)
        for project in projects:
            preview = self.processes.pop(project)
            await self._terminate(preview)
            if preview.root.is_dir():
                with suppress(OSError, ValueError):
                    async with resource_lock(
                        preview.root,
                        {"purpose": "preview_shutdown", "projectId": project},
                        recover_dead_local_owner=True,
                    ):
                        current = read_record(preview.root)
                        if current and current.get("nonce") == preview.nonce:
                            preview.status = "stopped"
                            self._persist(preview)
                            await self._clear_projection(preview)
        return projects


def re_full_hash(value):
    return len(value) == 20 and all(character in "0123456789abcdef" for character in value)
