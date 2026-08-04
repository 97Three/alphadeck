import asyncio
import logging
import os
import re
import signal
import sys
import time
from contextlib import suppress

import httpx

from .artifact_contracts import check_artifact_contracts
from .artifact_policy import check_artifact_policy
from .config import ROOT
from .data_prefetch import prefetch_data
from .generated_sandbox import generated_env, wrap_command
from .messages import js_slice
from .prefetch_market import timestamp
from .preview import persisted_target
from .scaffold import CATALOG, controlled_path, ensure_config, ensure_scaffold
from .validation_dashboard import check_binding, check_charts
from .validation_data import check_evidence, check_final_data
from .validation_inputs import (
    empty_screener,
    inspect_payload,
    planned_symbols,
    read_json,
    read_plan,
    read_text,
)
from .validation_repair import write_repair_plan
from .validation_reports import STALE_PATHS, current_run
from .visual_validation import validate_visual

REPORT = ".data-agent/validation.json"


def normalize_generated(root):
    ensure_config(root)


def signature(root):
    result = []
    for relative in STALE_PATHS:
        try:
            stat = controlled_path(root, relative).stat()
            result.append((relative, stat.st_size, stat.st_mtime_ns // 1000000))
        except FileNotFoundError:
            result.append((relative, None, None))
    return result


async def settle_artifacts(root, timeout=4, interval=0.5):
    deadline, previous, stable = time.monotonic() + timeout, None, 0
    while time.monotonic() < deadline:
        current = signature(root)
        stable = stable + 1 if current == previous else 0
        if stable >= 2:
            return
        previous = current
        await asyncio.sleep(interval)


async def prepare_validation(
    project, artifacts, transport=None, *, settle=True, assert_active=None
):
    if settle:
        await settle_artifacts(artifacts.root)
    plan, data = (
        read_plan(artifacts.root),
        read_json(artifacts.root, "data_file/final/dashboard-data.json"),
    )
    if plan is not None and transport is not None:
        inspection = inspect_payload(data)
        missing = [
            symbol for symbol in planned_symbols(plan) if symbol not in inspection["fetchedSymbols"]
        ]
        if not ((inspection["hasUsableMarketData"] or empty_screener(data)) and not missing):
            try:
                await prefetch_data(plan, transport, artifacts, assert_active=assert_active)
            except Exception:
                logging.warning(
                    "Unable to prepare missing final data before validation", exc_info=True
                )

    async def prepare(_tx):
        ensure_scaffold(artifacts.root, project)
        normalize_generated(artifacts.root)

    await artifacts.mutate(prepare)
    if settle:
        await settle_artifacts(artifacts.root)
    return artifacts.root


class GeneratedCommandRunner:
    def __init__(self, command_factory=None):
        self.command_factory = command_factory

    async def run(self, root, arguments, timeout=180):
        executable, compiler_arguments = "npm", arguments
        package = read_json(root, "package.json")
        if (
            arguments[:2] == ["run", "build"]
            and isinstance(package, dict)
            and package.get("scripts", {}).get("build") == "vite build --configLoader runner"
        ):
            executable = "node"
            compiler_arguments = [str(ROOT / "web/node_modules/vite/bin/vite.js"), "build", "--configLoader", "runner"]
        command = (
            self.command_factory(root, arguments)
            if self.command_factory
            else wrap_command(root, executable, compiler_arguments)
        )
        child = await asyncio.create_subprocess_exec(
            *command,
            cwd=root,
            env=generated_env(root, {"NODE_ENV": "production"}),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
        )
        output, timed_out = bytearray(), False

        async def collect():
            while chunk := await child.stdout.read(4096):
                output.extend(chunk)
                if len(output) > 96000:
                    del output[:-48000]
            await child.wait()

        try:
            try:
                async with asyncio.timeout(timeout):
                    await collect()
            except TimeoutError:
                timed_out = True
        finally:
            if child.returncode is None:
                with suppress(ProcessLookupError):
                    if sys.platform == "win32":
                        child.terminate()
                    else:
                        os.killpg(child.pid, signal.SIGTERM)
                try:
                    async with asyncio.timeout(5):
                        await child.wait()
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        if sys.platform == "win32":
                            child.kill()
                        else:
                            os.killpg(child.pid, signal.SIGKILL)
                    await child.wait()
        text = output.decode("utf-8", "replace").strip()
        tail = js_slice(text, -12000)
        if tail != text:
            tail = "...输出已截断，仅保留最后 12000 字符...\n" + tail
        return {
            "exitCode": child.returncode
            if child.returncode is not None and child.returncode >= 0
            else None,
            "signal": signal.Signals(-child.returncode).name
            if child.returncode is not None and child.returncode < 0
            else None,
            "timedOut": timed_out,
            "output": tail,
        }


async def check_build(artifacts, runner, timeout=180):
    async def normalize(_tx):
        normalize_generated(artifacts.root)

    await artifacts.mutate(normalize)
    raw = read_text(artifacts.root, "package.json")
    if not raw:
        return {"status": "failed", "summary": "未找到 package.json，无法执行 Vue build。"}
    package = read_json(artifacts.root, "package.json")
    if not isinstance(package, dict):
        return {"status": "failed", "summary": "package.json 不是有效 JSON。"}
    if not isinstance(package.get("scripts"), dict) or not package["scripts"].get("build"):
        return {"status": "failed", "summary": "package.json 缺少 build 脚本。"}
    result = await runner.run(artifacts.root, ["run", "build"], timeout)
    if result["exitCode"] == 0 and not result["timedOut"]:
        return {"status": "passed", "summary": "Vue build 通过。", "details": result["output"]}
    return {
        "status": "failed",
        "summary": f"Vue build 超过 {timeout:.1f}s 未完成。"
        if result["timedOut"]
        else f"Vue build 失败，退出码：{result['exitCode'] if result['exitCode'] is not None else 'null'}，信号：{result['signal'] or 'none'}。",
        "details": result["output"],
    }


async def local_response(url, *, timeout=5, maximum=2097152):
    parsed = httpx.URL(url)
    base = f"{parsed.scheme}://{parsed.host}:{parsed.port}"
    if not persisted_target(base, parsed.port):
        raise ValueError("Validation only probes managed local preview ports")
    async with (
        httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
        client.stream("GET", url, timeout=timeout) as response,
    ):
        parts, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > maximum:
                raise ValueError("Preview response exceeds its validation budget")
            parts.append(chunk)
        return response.status_code, b"".join(parts).decode("utf-8", "replace")


async def check_preview(project, previews, timeout=45):
    preview = await previews.start(project)
    if not preview.get("url"):
        return {
            "status": "failed",
            "summary": "预览服务未返回可访问 URL。",
            "metadata": {"preview": preview},
        }
    deadline, last, text = time.monotonic() + timeout, 0, ""
    while time.monotonic() < deadline:
        try:
            last, text = await local_response(preview["url"])
            if 200 <= last < 300:
                return {
                    "status": "passed",
                    "summary": f"预览首页 HTTP {last}。",
                    "metadata": {
                        "url": preview["url"],
                        "port": preview["port"],
                        "responsePreview": js_slice(text, 0, 400),
                    },
                }
        except Exception as cause:
            text = str(cause)
        await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError(
        f"预览地址未返回 2xx，最后状态码：{last}，响应：{js_slice(text, 0, 500)}"
        if last
        else f"预览地址未在 {timeout * 1000:g}ms 内返回 HTTP 200：{text}"
    )


async def check_visual(project, artifacts, previews, request=None):
    preview = await previews.start(project)
    if not preview.get("url"):
        return {"status": "failed", "summary": "无法执行视觉验收，因为预览 URL 不存在。"}
    report = await validate_visual(project, artifacts, preview["url"], request)
    metadata = {
        "reportPath": report["reportPath"],
        "screenshotDir": report["screenshotDir"],
        "viewports": [
            {key: viewport[key] for key in ("id", "screenshotPath", "metrics")}
            for viewport in report["viewports"]
        ],
    }
    if not report["passed"]:
        screenshots = "截图：" + "；".join(
            viewport["id"] + "=" + viewport["screenshotPath"] for viewport in report["viewports"]
        )
        return {
            "status": "failed",
            "summary": f"视觉验收未通过：{len(report['failures'])} 个阻断项。",
            "details": "\n".join(
                [*report["failures"], *([screenshots] if report["viewports"] else [])]
            ),
            "metadata": metadata,
        }
    return {
        "status": report["status"],
        "summary": f"视觉验收通过但有 {len(report['warnings'])} 个警告。"
        if report["status"] == "warning"
        else "桌面和移动端视觉验收通过。",
        **({"details": "\n".join(report["warnings"])} if report["warnings"] else {}),
        "metadata": metadata,
    }


async def check_market_proxy(project, artifacts, previews):
    root = artifacts.root
    route = "vite.config.ts"
    if read_text(root, route) != CATALOG["files"][route]:
        return {
            "status": "failed",
            "summary": "Python 行情桥接配置缺失或被修改。",
            "details": "由平台恢复 vite.config.ts 的 /api/market 转发配置，再重新验收。",
        }
    preview = await previews.start(project)
    if not preview.get("url"):
        return {
            "status": "failed",
            "summary": "无法检查 /api/market 代理，因为预览 URL 不存在。",
            "metadata": {"route": route},
        }
    url = preview["url"].rstrip("/") + "/api/market/quotes/realtime/600519"
    status, text = await local_response(url, timeout=8)
    metadata = {"route": route, "probeUrl": url}
    if not 200 <= status < 300:
        return {
            "status": "failed",
            "summary": f"/api/market 代理未返回 2xx，状态码：{status}。",
            "details": js_slice(text, 0, 1000),
            "metadata": metadata,
        }
    if not re.search("600519|贵州茅台|price|symbol|quote|latest|fetched_at|source", text, re.I):
        return {
            "status": "failed",
            "summary": "/api/market 代理返回了 2xx，但响应不像真实行情数据。",
            "details": js_slice(text, 0, 1000),
            "metadata": metadata,
        }
    return {
        "status": "passed",
        "summary": "/api/market 同源代理可用，实时行情探测通过。",
        "metadata": metadata,
    }


async def run_check(identity, name, operation):
    start = time.monotonic()
    try:
        value = operation()
        result = await value if hasattr(value, "__await__") else value
    except Exception as cause:
        result = {"status": "failed", "summary": name + "检查异常。", "details": str(cause)}
    return {
        "id": identity,
        "name": name,
        "durationMs": max(0, int((time.monotonic() - start) * 1000)),
        **result,
    }


class ProjectValidator:
    def __init__(self, previews, *, runner=None, transport=None, publish=None):
        self.previews, self.runner, self.transport, self.publish = (
            previews,
            runner or GeneratedCommandRunner(),
            transport,
            publish,
        )
        self.locks = {}

    async def validate(self, project, artifacts, request=None, *, prepared=False):
        async with self.locks.setdefault(project, asyncio.Lock()):
            if not prepared:
                await prepare_validation(project, artifacts, self.transport)
            now = timestamp()
            with suppress(Exception):
                await self.previews.stop(project)
            await artifacts.event(
                {
                    "event_type": "validation_started",
                    "stage": "validation",
                    "status": "pending",
                    **({"run_id": request} if request else {}),
                    "summary": "开始自动验证：build、HTTP 200、最终数据文件、evidence、产物策略、图表和 /api/market 代理。",
                    "created_at": now,
                }
            )
            checks = []
            try:
                policy = await run_check(
                    "artifact_policy", "生成产物策略", lambda: check_artifact_policy(artifacts.root)
                )
                checks.append(policy)
                executable = [
                    ("vue_build", "Vue build", lambda: check_build(artifacts, self.runner)),
                    (
                        "preview_http_200",
                        "预览 HTTP 200",
                        lambda: check_preview(project, self.previews),
                    ),
                    (
                        "visual_presentation",
                        "视觉验收",
                        lambda: check_visual(project, artifacts, self.previews, request),
                    ),
                ]
                for identity, name, operation in executable:
                    checks.append(
                        await run_check(identity, name, operation)
                        if policy["status"] != "failed"
                        else {
                            "id": identity,
                            "name": name,
                            "status": "warning",
                            "summary": "生成产物安全预检失败，已跳过可执行检查。",
                            "details": "修复 artifact_policy 后才会执行生成代码。",
                            "durationMs": 0,
                        }
                    )
                for identity, name, operation in [
                    ("final_data_file", "最终数据文件", lambda: check_final_data(artifacts.root)),
                    ("evidence_files", "数据证据文件", lambda: check_evidence(artifacts)),
                    (
                        "artifact_contracts",
                        "产物 Schema 契约",
                        lambda: check_artifact_contracts(project, artifacts, request),
                    ),
                    (
                        "dashboard_data_binding",
                        "页面数据绑定",
                        lambda: check_binding(artifacts.root),
                    ),
                    ("chart_presence", "金融图表存在性", lambda: check_charts(artifacts.root)),
                    (
                        "market_proxy",
                        "/api/market 代理",
                        lambda: check_market_proxy(project, artifacts, self.previews),
                    ),
                ]:
                    checks.append(await run_check(identity, name, operation))
            finally:
                with suppress(Exception):
                    await self.previews.stop(project)
            passed = all(check["status"] != "failed" for check in checks)
            updated = timestamp()
            report = {
                "schemaVersion": 1,
                **(
                    {"runId": request or current_run(artifacts.root)}
                    if request or current_run(artifacts.root)
                    else {}
                ),
                "status": "passed" if passed else "failed",
                "passed": passed,
                "projectId": project,
                "reportPath": REPORT,
                "checks": checks,
                "createdAt": now,
                "updatedAt": updated,
            }
            await artifacts.write(REPORT, report)
            await write_repair_plan(artifacts, report)
            await artifacts.event(
                {
                    "event_type": "validation_completed",
                    "stage": "validation",
                    "status": "success" if passed else "error",
                    **({"run_id": request} if request else {}),
                    "artifact_path": REPORT,
                    "summary": "自动验证通过。"
                    if passed
                    else f"自动验证未通过：{sum(check['status'] == 'failed' for check in checks)} 项失败。",
                    "created_at": updated,
                }
            )
            if self.publish:
                await self.publish(project, report)
            return report
