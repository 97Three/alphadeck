import os
import re
from urllib.parse import urlsplit
from uuid import uuid4

from .config import ROOT
from .preview import persisted_target
from .workspace import write_atomic


def check_metrics(info, case, viewport):
    failures = []
    for keyword in case.get("expectedVisualKeywords", ["AlphaDeck"]):
        if keyword not in info["text"]:
            failures.append(viewport + " 页面缺少关键词：" + keyword)
    if not (info["svgCount"] + info["canvasCount"] > 0 or info["rectCount"] >= 12):
        failures.append(viewport + " 页面缺少可识别图表元素。")
    if info["horizontalOverflow"]:
        failures.append(viewport + " 页面存在水平溢出。")
    if info["headingCount"] > 1:
        failures.append(f"{viewport} 页面存在 {info['headingCount']} 个 h1，应最多保留一个主标题。")
    if info["accessibilityIssueCount"]:
        failures.append(
            f"{viewport} 页面存在 {info['accessibilityIssueCount']} 个无可访问名称的控件或图片。"
        )
    return failures


async def run_visual_check(runner, project, case):
    if not case.get("visualCheck"):
        return None
    from playwright.async_api import async_playwright

    failures, screenshots, issues, preview = [], [], 0, None
    try:
        preview = await runner.previews.start(project)
        origin = urlsplit(preview["url"])
        if not persisted_target(preview["url"], origin.port):
            raise ValueError("Benchmark visual check requires a managed local preview")
        async with async_playwright() as playwright:
            options = {"headless": True}
            if os.getenv("ALPHADECK_CHROMIUM_EXECUTABLE_PATH"):
                options["executable_path"] = os.environ["ALPHADECK_CHROMIUM_EXECUTABLE_PATH"]
            browser = await playwright.chromium.launch(**options)
            try:
                for identity, width, height in (("desktop", 1440, 1000), ("mobile", 390, 844)):
                    page = await browser.new_page(
                        viewport={"width": width, "height": height}, device_scale_factor=1
                    )
                    resources, errors = [], []

                    def resource(response, resources=resources):
                        kind = response.request.resource_type
                        if (
                            response.status >= 400
                            and kind in ("document", "script", "stylesheet", "font", "image")
                            and len(resources) < 100
                        ):
                            resources.append(f"{response.status} {kind} {response.url[:2000]}")

                    async def route(intercepted):
                        target = urlsplit(intercepted.request.url)
                        if target.scheme in ("data", "blob") or (
                            (target.scheme, target.hostname, target.port)
                            == (origin.scheme, origin.hostname, origin.port)
                            and intercepted.request.method in ("GET", "HEAD")
                        ):
                            await intercepted.continue_()
                        else:
                            await intercepted.abort()

                    page.on("response", resource)
                    page.on(
                        "pageerror",
                        lambda error, errors=errors: (
                            errors.append(str(error)[:2000]) if len(errors) < 100 else None
                        ),
                    )
                    await page.route("**/*", route)
                    if hasattr(page, "route_web_socket"):
                        await page.route_web_socket("**/*", lambda socket: socket.close())
                    try:
                        response = await page.goto(
                            preview["url"], wait_until="networkidle", timeout=45000
                        )
                        if response is None or not response.ok:
                            failures.append(identity + " 预览地址未返回 2xx。")
                        name = re.sub(r"[^a-zA-Z0-9_-]", "-", case["id"])[:80]
                        path = (
                            "tmp/alphadeck-benchmark-screenshots/"
                            + name
                            + "-"
                            + identity
                            + "-"
                            + uuid4().hex
                            + ".png"
                        )
                        content = await page.screenshot(full_page=True, timeout=15000)

                        async def save(_tx, path=path, content=content):
                            return write_atomic(ROOT, path, content)

                        await runner.authority(save)
                        screenshots.append(
                            {"viewport": identity, "path": path, "width": width, "height": height}
                        )
                        info = await page.evaluate(
                            (ROOT / "config/benchmark-visual-metrics.js").read_text()
                        )
                        failures.extend(check_metrics(info, case, identity))
                        issues += info["accessibilityIssueCount"]
                        failures.extend(identity + " 资源加载失败：" + value for value in resources)
                        failures.extend(identity + " 页面运行时错误：" + value for value in errors)
                    finally:
                        await page.close()
            finally:
                await browser.close()
    except Exception as error:
        failures.append("视觉检查无法执行：" + str(error))
    return {
        "passed": not failures,
        "failures": failures,
        "screenshots": screenshots,
        "screenshotPath": screenshots[0]["path"] if screenshots else None,
        "accessibilityIssueCount": issues,
        "previewUrl": preview["url"] if preview else None,
    }
