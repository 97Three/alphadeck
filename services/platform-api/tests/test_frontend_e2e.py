"""Real Next pages, BetterAuth client, Python API/Worker, compiler and Chromium.

Only upstream model semantics and market responses are controlled fixtures.
Run explicitly with PLATFORM_FRONTEND_E2E=1 against an isolated test database.
"""

import asyncio
import copy
import json
import os
import re
import signal
import socket
import sys
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright
from test_project_creation import repository  # noqa: F401

from alphadeck_platform.agent_executor import AgentExecutor
from alphadeck_platform.auth import Authorization
from alphadeck_platform.config import ROOT
from alphadeck_platform.credentials import AuthOptions
from alphadeck_platform.database import Database
from alphadeck_platform.finance_generation import FinanceGenerationRuntime
from alphadeck_platform.generation_worker import GenerationWorker
from alphadeck_platform.mission_store import Missions
from alphadeck_platform.prefetch_transport import MarketTransport
from alphadeck_platform.preview import PreviewManager
from alphadeck_platform.projects import Projects
from alphadeck_platform.provision import Provisioner
from app import create_app

FIXTURES = Path(__file__).parent / "fixtures"


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(20):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20)
        sock.close()


@pytest.mark.skipif(
    os.getenv("PLATFORM_FRONTEND_E2E") != "1", reason="Explicit full frontend integration run"
)
def test_original_frontend_with_only_python_business_backend(
    database_url,
    actor,
    tmp_path,
    repository,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    if sys.platform != "linux":
        pytest.skip("Requires the generated-project Linux namespace runtime")

    async def run():
        database = Database(database_url)
        password = "Migration-Only-Password-482!"
        hashed = await AuthOptions().hash_password(password)
        async with database.transaction() as tx:
            await tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))
            await tx.execute(
                "INSERT INTO auth_accounts (id,user_id,account_id,provider_id,password,updated_at) VALUES (%s,%s,%s,'credential',%s,now())",
                (str(uuid4()), actor["user_id"], actor["user_id"], hashed),
            )
        market, upstream_calls = FastAPI(), []
        fixture = json.loads((FIXTURES / "data-prefetch.json").read_text())[0]
        # This UI scenario needs the renderer's real 20-bar prerequisite.
        # The small parser fixture has only two rows, so supply a declared test series.
        latest = datetime.now(UTC).date()
        days = [
            latest - timedelta(days=offset)
            for offset in range(90)
            if (latest - timedelta(days=offset)).weekday() < 5
        ][:41][::-1]
        for response in fixture["responses"]:
            if "/quotes/history/" in response["request"]["path"]:
                response["payload"]["bars"] = [
                    {
                        "date": day.isoformat(),
                        "open": 99 + index * 0.5,
                        "high": 102 + index * 0.5,
                        "low": 98 + index * 0.5,
                        "close": 100 + index * 0.5,
                        "volume": 200 + index,
                        "amount": (100 + index * 0.5) * (200 + index),
                    }
                    for index, day in enumerate(days)
                ]

        @market.api_route("/api/v1/{path:path}", methods=["GET", "POST"])
        async def controlled_market(request: Request, path: str):
            upstream_calls.append(request.url.path)
            response = next(
                (row for row in fixture["responses"] if row["request"]["path"] == request.url.path),
                None,
            )
            if response:
                return JSONResponse(response["payload"], status_code=response["status"])
            if "symbols" in path:
                return {
                    "results": [
                        {
                            "symbol": "600519",
                            "name": "贵州茅台",
                            "market": "SH",
                            "asset_type": "stock",
                        }
                    ]
                }
            return JSONResponse(
                {"detail": "Controlled upstream: endpoint unavailable"}, status_code=404
            )

        case = next(
            row
            for row in json.loads((FIXTURES / "finance-planning.json").read_text())["cases"]
            if row["name"] == "stock"
        )

        async def rewrite(query, options):
            value = copy.deepcopy(case["params"]["queryRewrite"])
            value["originalQuery"] = query
            return value

        async def ready():
            return {
                "installed": True,
                "configured": True,
                "checking": False,
                "version": "controlled-upstream",
                "available": True,
                "models": ["deepseek-v4-flash"],
                "modelChecks": [
                    {
                        "id": "deepseek-v4-flash",
                        "configured": True,
                        "status": "available",
                        "message": "Controlled upstream fixture",
                    }
                ],
            }

        monkeypatch.setattr("app.availability", ready)
        monkeypatch.setenv("ALPHADECK_AUTH_MODE", "local")
        monkeypatch.setenv("ALPHADECK_AUTH_SECRET", "test-secret")
        monkeypatch.setenv("ALPHADECK_MEMORY_ENABLED", "0")
        monkeypatch.setenv("ALPHADECK_KNOWLEDGE_ENABLED", "0")
        monkeypatch.setenv("PI_AGENT_DISPATCH_MODE", "worker")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "controlled-test-key-never-used")
        projects = Projects(database, tmp_path / "projects", Provisioner(repository))
        previews = PreviewManager(projects)
        application = create_app(
            database,
            Authorization(database, enabled=True, secret="test-secret"),
            projects_dir=tmp_path / "projects",
            project_provisioner=Provisioner(repository),
            preview_manager=previews,
            query_rewriter=rewrite,
            skills_root=repository,
        )
        project_ids = []
        async with serve(market) as market_url, serve(application) as platform_url:
            monkeypatch.setenv("ALPHADECK_MARKET_API_URL", market_url)
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            web_port = probe.getsockname()[1]
            probe.close()
            origin = f"http://127.0.0.1:{web_port}"
            monkeypatch.setenv("NEXT_PUBLIC_APP_URL", origin)
            monkeypatch.setenv("BETTER_AUTH_URL", origin)
            environment = {
                **os.environ,
                "ALPHADECK_PLATFORM_API_URL": platform_url,
                "NEXT_TELEMETRY_DISABLED": "1",
                "NODE_ENV": "development",
            }
            # Prove that the frontend never needs a direct database connection.
            environment.pop("DATABASE_URL", None)
            with (tmp_path / "frontend.log").open("wb") as logs:
                child = await asyncio.create_subprocess_exec(
                    "node",
                    str(ROOT / "node_modules/next/dist/bin/next"),
                    "dev",
                    "--webpack",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(web_port),
                    cwd=ROOT,
                    env=environment,
                    stdout=logs,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    async with httpx.AsyncClient(trust_env=False) as client:
                        async with asyncio.timeout(360):
                            while True:
                                if child.returncode is not None:
                                    pytest.fail((tmp_path / "frontend.log").read_text()[-6000:])
                                try:
                                    if (
                                        await client.get(origin + "/login", timeout=5)
                                    ).status_code == 200:
                                        break
                                except httpx.HTTPError:
                                    pass
                                await asyncio.sleep(0.2)
                    async with async_playwright() as playwright:
                        options = {"headless": True, "args": ["--no-sandbox"]}
                        if os.getenv("ALPHADECK_CHROMIUM_EXECUTABLE_PATH"):
                            options["executable_path"] = os.environ[
                                "ALPHADECK_CHROMIUM_EXECUTABLE_PATH"
                            ]
                        browser = await playwright.chromium.launch(**options)
                        try:
                            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                            page.set_default_timeout(60000)
                            errors = []
                            page.on("pageerror", lambda error: errors.append(str(error)))
                            await page.goto(origin, wait_until="domcontentloaded", timeout=120000)
                            assert "/login" in page.url
                            await page.wait_for_function(
                                "() => Object.keys(document.querySelector('#identity') || {}).some(key => key.startsWith('__reactProps'))",
                                timeout=90000,
                            )
                            await page.get_by_label("账号或邮箱").fill(
                                actor["user_id"] + "@example.test"
                            )
                            await page.get_by_label("密码", exact=True).fill(password)
                            await page.get_by_role("button", name="登录", exact=True).click()
                            await page.wait_for_url(origin + "/", timeout=60000)
                            for path in (
                                "/skills",
                                "/business-knowledge",
                                "/strategy-platform",
                                "/research-reports",
                                "/eval-platform",
                                "/ops-platform",
                                "/admin/users",
                                "/account/usage",
                                "/account/memory",
                            ):
                                response = await page.goto(
                                    origin + path, wait_until="domcontentloaded", timeout=120000
                                )
                                assert response.status == 200, (path, response.status)
                                await page.locator("main").first.wait_for()
                                assert (
                                    "Application error"
                                    not in await page.locator("body").inner_text()
                                ), path
                            await page.goto(origin, wait_until="domcontentloaded")
                            model_selector = page.get_by_role("combobox", name="选择分析模型")
                            await page.wait_for_function(
                                "() => [...document.querySelectorAll('[role=combobox]')].some(node => node.getAttribute('aria-label') === '选择分析模型' && Object.keys(node).some(key => key.startsWith('__reactProps')))"
                            )
                            if "Official Direct" not in await model_selector.inner_text():
                                await model_selector.click()
                                await page.get_by_role(
                                    "option",
                                    name="DeepSeek V4 Flash (Official Direct)",
                                    exact=False,
                                ).click()
                            await page.get_by_label("量化分析需求").fill(
                                "分析贵州茅台趋势，生成标准看板"
                            )
                            await page.get_by_role("button", name="提交任务", exact=True).click()
                            await page.wait_for_url(
                                re.compile(r"/[^/]+/chat"),
                                timeout=240000,
                                wait_until="domcontentloaded",
                            )
                            project = page.url.split("/")[-2]
                            project_ids.append(project)
                            async with asyncio.timeout(60):
                                while True:
                                    job = await database.fetch_one(
                                        "SELECT * FROM agent_generation_jobs WHERE project_id=%s ORDER BY created_at DESC LIMIT 1",
                                        (project,),
                                    )
                                    if job:
                                        break
                                    await asyncio.sleep(0.2)
                            assert await page.evaluate("""() => new Promise((resolve, reject) => {
                                const socket = new WebSocket(location.origin.replace('http', 'ws') + '/api/ws/' + location.pathname.split('/')[1]);
                                const timer = setTimeout(() => { socket.close(); reject(new Error('WS timeout')); }, 15000);
                                socket.onmessage = event => {
                                    if (event.data === 'pong') { clearTimeout(timer); socket.close(); resolve(true); }
                                    else if (JSON.parse(event.data).type === 'connected') socket.send('ping');
                                }; socket.onerror = () => { clearTimeout(timer); reject(new Error('WS handshake failed')); };
                            })""")
                            assert await page.evaluate("""() => new Promise((resolve, reject) => {
                                window.__nativeStreamEvents = []; window.__nativeStreamErrors = 0;
                                const stream = window.__nativeStream = new EventSource('/api/chat/' + location.pathname.split('/')[1] + '/stream');
                                const timer = setTimeout(() => { stream.close(); reject(new Error('SSE connection timeout')); }, 15000);
                                stream.onmessage = event => { const value = JSON.parse(event.data); window.__nativeStreamEvents.push(value.type); if (value.type === 'connected') { clearTimeout(timer); resolve(true); } };
                                stream.onerror = () => { window.__nativeStreamErrors += 1; };
                            })""")
                            runtime = FinanceGenerationRuntime(
                                database,
                                projects,
                                previews,
                                rewriter=rewrite,
                                transport=MarketTransport(base_url=market_url),
                                executor=AgentExecutor(
                                    projects,
                                    provider_factory=lambda _: pytest.fail(
                                        "Standard fixture must never contact an external model"
                                    ),
                                    skill_root=repository,
                                ),
                            )
                            worker = GenerationWorker(
                                database, runtime.registry(), pool="browser.e2e." + uuid4().hex
                            )
                            async with asyncio.timeout(420):
                                assert await worker.execute_job(job, "browser-e2e-owner")
                            terminal = await database.fetch_one(
                                "SELECT status,error_message FROM agent_generation_jobs WHERE id=%s",
                                (job["id"],),
                            )
                            assert terminal["status"] == "completed", terminal
                            accepted = await Missions(database).accepted_snapshot(
                                project, job["request_id"]
                            )
                            assert (
                                accepted["acceptedReceiptId"]
                                and accepted["missionStatus"] == "completed"
                            )
                            root = tmp_path / "projects" / project
                            report = json.loads((root / ".data-agent/validation.json").read_text())
                            assert report["passed"], report
                            delivery = await page.request.get(
                                origin + "/api/projects/" + project + "/generation/status"
                            )
                            delivery_value = await delivery.json()
                            (tmp_path / "final-delivery.json").write_text(
                                json.dumps(delivery_value, ensure_ascii=False)
                            )
                            assert delivery_value["data"]["status"] == "ready", delivery_value
                            visual = json.loads(
                                (root / ".data-agent/visual-validation.json").read_text()
                            )
                            assert len(visual["viewports"]) == 2
                            assert (
                                upstream_calls
                                and "/api/v1/quotes/realtime/600519" in upstream_calls
                            )
                            assert not (root / "app/api/market/[...path]/route.ts").exists()
                            await page.locator("iframe").first.wait_for(timeout=90000)
                            await (
                                page.frame_locator("iframe")
                                .first.get_by_text("600519", exact=False)
                                .first.wait_for(timeout=90000)
                            )
                            stream_result = await page.evaluate(
                                "() => ({ events: window.__nativeStreamEvents, errors: window.__nativeStreamErrors })"
                            )
                            assert (
                                stream_result["errors"] == 0
                                and "message" in stream_result["events"]
                            ), stream_result
                            await page.evaluate("() => window.__nativeStream.close()")
                            await page.screenshot(
                                path=str(tmp_path / "completed-chat.png"), full_page=True
                            )
                            assert not errors, errors
                        except BaseException as error:
                            (tmp_path / "browser-errors.json").write_text(
                                json.dumps([str(error), *errors], ensure_ascii=False)
                            )
                            with suppress(Exception):
                                (tmp_path / "browser-page.html").write_text(await page.content())
                                await page.screenshot(
                                    path=str(tmp_path / "failed-page.png"), full_page=True
                                )
                            raise
                        finally:
                            await browser.close()
                finally:
                    await previews.cleanup_all()
                    if child.returncode is None:
                        with suppress(ProcessLookupError):
                            os.killpg(child.pid, signal.SIGTERM)
                        try:
                            await asyncio.wait_for(child.wait(), 10)
                        except TimeoutError:
                            with suppress(ProcessLookupError):
                                os.killpg(child.pid, signal.SIGKILL)
                            await child.wait()
                    async with database.transaction() as tx:
                        await tx.execute(
                            "DELETE FROM projects WHERE id=ANY(%s) OR owner_id=%s",
                            (project_ids, actor["user_id"]),
                        )

    asyncio.run(run())
