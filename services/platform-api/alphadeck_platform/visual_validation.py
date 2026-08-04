import asyncio
import os
import re
from contextlib import suppress
from urllib.parse import urlsplit

from .config import ROOT
from .prefetch_market import timestamp
from .preview import persisted_target

REPORT = ".data-agent/visual-validation.json"
SCREENSHOTS = "tmp/visual-checks"
VIEWPORTS = [
    {"id": "desktop", "width": 1440, "height": 900},
    {"id": "mobile", "width": 390, "height": 844},
]


def workbench_surface(metrics):
    dominant = (
        metrics["cardGridClusterCount"] > 0
        or metrics["firstViewportCardLikeSurfaceCount"] >= 4
        and metrics["firstViewportCardLikeSurfaceRatio"] >= 0.65
        or metrics["cardLikeSurfaceCount"] >= 8
        and metrics["cardLikeSurfaceRatio"] >= 0.6
    )
    if dominant:
        return {
            "failures": [
                "页面由独立圆角卡片网格主导；金融看板应使用连续工作台画布，以分区线、数据带、主图、矩阵和表格组织内容。"
            ],
            "warnings": [],
        }
    if metrics["firstViewportCardLikeSurfaceCount"] >= 4 or metrics["cardLikeSurfaceCount"] >= 8:
        return {
            "failures": [],
            "warnings": [
                "页面独立卡片式容器偏多，建议合并为连续数据区并减少圆角、阴影和重复外框。"
            ],
        }
    return {"failures": [], "warnings": []}


def core_visual(metrics):
    recognizable = (
        metrics["svgCount"] + metrics["canvasCount"] > 0
        or metrics["rectCount"] >= 12
        or metrics["visibleGraphicCount"] >= 12
    )
    return (
        []
        if recognizable or metrics["firstViewportTableCount"] > 0
        else ["页面缺少可识别的图表元素。"]
    )


def assess_metrics(metrics, viewport):
    failures, warnings = [], []
    for condition, message in (
        (
            metrics["blankLike"] or metrics["textLength"] < 120,
            "首屏内容过少，页面疑似空白或未完成渲染。",
        ),
        (metrics["horizontalOverflow"], "页面存在横向溢出。"),
        (not metrics["hasMarketLanguage"], "页面缺少行情、K 线、财务、风险或持仓等金融语义。"),
        (
            not metrics["firstViewportHasMarketLanguage"]
            or metrics["firstViewportTextLength"] < 80,
            "首屏缺少真实金融数据、行情指标或可用分析内容。",
        ),
        (metrics["oversizedHeroLike"], "首屏疑似营销式大标题或空 hero，占用了核心金融内容位置。"),
    ):
        if condition:
            failures.append(message)
    if (
        viewport == "desktop"
        and metrics["firstViewportGraphicCount"] + metrics["firstViewportTableCount"] == 0
    ):
        warnings.append("桌面首屏没有图表或表格，可能需要把核心可视化上移。")
    if not metrics["firstViewportHasCoreVisual"]:
        failures.append("首屏没有可用的核心图表、矩阵或表格；迷你 sparkline/装饰图不能替代主图。")
    if metrics["largeChartCount"] == 0 and metrics["firstViewportTableCount"] == 0:
        failures.append("页面没有检测到足够尺寸的金融主图或数据矩阵。")
    if metrics["tinyChartCount"] >= 3 and metrics["largeChartCount"] == 0:
        failures.append("页面主要由迷你图组成，缺少带坐标/刻度/上下文的主图。")
    if metrics["squashedMetricCount"] > 0:
        failures.append(
            f"检测到 {metrics['squashedMetricCount']} 个数字或指标被挤压成多行，图表或数据区宽度需要调整。"
        )
    if viewport == "desktop" and metrics["orphanedMetricRowCount"] > 0:
        failures.append(
            f"检测到 {metrics['orphanedMetricRowCount']} 个指标带末行只有一个窄指标，产生大面积空白；请按指标数量采用均衡网格或让末项填满整行。"
        )
    surface = workbench_surface(metrics)
    failures.extend(surface["failures"])
    warnings.extend(surface["warnings"])
    if not metrics["hasDataFreshnessLanguage"]:
        warnings.append("页面缺少数据更新时间、报告期或样本口径说明。")
    failures.extend(core_visual(metrics))
    if metrics["textBlockCount"] < 6:
        warnings.append("页面可读文本块较少，可能缺少摘要、指标或说明。")
    return failures, warnings


def issue_status(failures, warnings):
    return "failed" if failures else "warning" if warnings else "passed"


def infrastructure_error(error):
    return bool(
        re.search(
            r"Executable doesn't exist|playwright install|No module named ['\"]playwright|Cannot find (?:module|package).*playwright|browserType\.launch|BrowserType\.launch",
            str(error),
            re.I,
        )
    )


def ignored_console_error(message):
    # The validator closes WebSockets, so Vite's dev client reports this even
    # when the dashboard itself loaded successfully.
    return message.startswith("[vite] failed to connect to websocket.")


async def viewport_result(browser, artifacts, project, preview_url, stamp, viewport, probe):
    page = await browser.new_page(
        viewport={key: viewport[key] for key in ("width", "height")},
        device_scale_factor=1,
        is_mobile=viewport["id"] == "mobile",
    )
    console, errors, resources = [], [], []

    def on_console(message):
        if ignored_console_error(message.text):
            return
        if message.type == "error" and len(console) < 8:
            console.append(message.text[:2000])

    def on_page_error(error):
        if len(errors) < 100:
            errors.append(str(error)[:2000])

    def on_response(response):
        resource = response.request.resource_type
        if (
            response.status >= 400
            and resource in {"document", "script", "stylesheet", "font", "image"}
            and len(resources) < 100
        ):
            resources.append(f"{response.status} {resource} {response.url[:2000]}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("response", on_response)
    origin = urlsplit(preview_url)

    async def route(request_route):
        target = urlsplit(request_route.request.url)
        if (
            target.scheme in {"data", "blob"}
            or (target.scheme, target.hostname, target.port)
            == (origin.scheme, origin.hostname, origin.port)
            and request_route.request.method in {"GET", "HEAD"}
        ):
            await request_route.continue_()
        else:
            await request_route.abort()

    await page.route("**/*", route)
    if hasattr(page, "route_web_socket"):
        await page.route_web_socket("**/*", lambda websocket: websocket.close())
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", project)[:80] or "visual"
    screenshot = f"{SCREENSHOTS}/{name}-{viewport['id']}-{stamp}.png"
    failures, warnings = [], []
    try:
        response = await page.goto(preview_url, wait_until="domcontentloaded", timeout=45000)
        if response is None or not response.ok:
            failures.append(
                "预览地址未返回 2xx：" + (str(response.status) if response else "无响应")
            )
        with suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=8000)
        await artifacts.screenshot(screenshot, await page.screenshot(full_page=True, timeout=15000))
        metrics = await page.evaluate(probe)
        metric_failures, metric_warnings = assess_metrics(metrics, viewport["id"])
        failures.extend(metric_failures)
        warnings.extend(metric_warnings)
        warnings.extend("console error：" + value for value in console)
        failures.extend("页面运行错误：" + value for value in errors)
        failures.extend("资源加载失败：" + value for value in resources)
        return {
            **viewport,
            "screenshotPath": screenshot,
            "status": issue_status(failures, warnings),
            "failures": list(dict.fromkeys(failures)),
            "warnings": list(dict.fromkeys(warnings)),
            "metrics": metrics,
        }
    finally:
        await page.close()


async def validate_visual(project, artifacts, preview_url, request=None):
    # A generated page cannot choose a remote or unrelated local browser target.
    parsed = urlsplit(preview_url)
    if not persisted_target(preview_url, parsed.port):
        raise ValueError("Visual validation requires a managed local preview URL")
    created, stamp, browser = timestamp(), re.sub(r"[:.]", "-", timestamp()), None
    viewports, failures, warnings = [], [], []
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            options = {"headless": True}
            if os.getenv("ALPHADECK_CHROMIUM_EXECUTABLE_PATH"):
                options["executable_path"] = os.environ["ALPHADECK_CHROMIUM_EXECUTABLE_PATH"]
            browser = await playwright.chromium.launch(**options)
            probe = (ROOT / "config/visual-dom-metrics.js").read_text()
            tasks = [
                asyncio.create_task(
                    viewport_result(
                        browser, artifacts, project, preview_url, stamp, viewport, probe
                    )
                )
                for viewport in VIEWPORTS
            ]
            try:
                viewports = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await browser.close()
            failures = list(
                dict.fromkeys(
                    viewport["id"] + "：" + value
                    for viewport in viewports
                    for value in viewport["failures"]
                )
            )
            warnings = list(
                dict.fromkeys(
                    viewport["id"] + "：" + value
                    for viewport in viewports
                    for value in viewport["warnings"]
                )
            )
    except Exception as cause:
        if infrastructure_error(cause):
            warnings = [
                "视觉截图验收已跳过：当前运行环境未安装 Playwright Chromium；运行 python -m playwright install chromium 后可恢复桌面端和移动端截图验收。"
            ]
        else:
            failures = ["视觉验收执行异常：" + str(cause)[:2000]]
    status, updated = issue_status(failures, warnings), timestamp()
    report = {
        "schemaVersion": 1,
        "projectId": project,
        "requestId": request,
        "status": status,
        "passed": status != "failed",
        "previewUrl": preview_url,
        "reportPath": REPORT,
        "screenshotDir": SCREENSHOTS,
        "viewports": viewports,
        "failures": failures,
        "warnings": warnings,
        "createdAt": created,
        "updatedAt": updated,
    }
    await artifacts.write(REPORT, report)
    await artifacts.event(
        {
            "event_type": "visual_validation_completed",
            "stage": "validation",
            "status": "error"
            if status == "failed"
            else "warning"
            if status == "warning"
            else "success",
            **({"run_id": request} if request is not None else {}),
            "artifact_path": REPORT,
            "summary": f"视觉验收未通过：{len(failures)} 个阻断项。"
            if status == "failed"
            else f"视觉验收通过但有 {len(warnings)} 个警告。"
            if status == "warning"
            else "视觉验收通过。",
            "created_at": updated,
        }
    )
    return report
