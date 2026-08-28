import asyncio
import json
import socket
from pathlib import Path

import pytest

from alphadeck_platform.prefetch_transport import WorkspaceArtifacts
from alphadeck_platform.preview import bounds
from alphadeck_platform.visual_validation import (
    core_visual,
    ignored_console_error,
    validate_visual,
    workbench_surface,
)

CASES = json.loads((Path(__file__).parent / "fixtures/visual-metrics.json").read_text())


def test_expected_vite_hmr_error_is_ignored_without_hiding_page_errors():
    assert ignored_console_error("[vite] failed to connect to websocket.\nyour current setup:")
    assert not ignored_console_error("Uncaught TypeError: Cannot read properties of undefined")


@pytest.mark.parametrize("case", CASES["surface"])
def test_surface_metrics_match_original(case):
    assert workbench_surface(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["core"])
def test_core_visual_metrics_match_original(case):
    assert core_visual(case["input"]) == case["expected"]


def test_browser_inspects_desktop_mobile_and_saves_real_screenshots(tmp_path):
    async def run():
        async def authority(operation):
            return await operation(None)

        artifacts = WorkspaceArtifacts(tmp_path, authority)
        text = "行情价格风险数据更新时间：2025-01-10。" * 20
        html = f"""<!doctype html><html lang="zh-CN"><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>body{{margin:0;font:14px sans-serif;}} main{{width:100%;}} table{{width:100%;border-collapse:collapse;}} td{{border-bottom:1px solid #ddd;}}</style></head><body><main data-visual-language="financial-workbench"><h1>行情分析</h1><p>{text}</p><table><thead><tr><th>标的</th><th>价格</th></tr></thead><tbody>{"".join("<tr><td>贵州茅台</td><td>100.00</td></tr>" for _ in range(12))}</tbody></table></main></body></html>""".encode()

        async def serve(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
                + str(len(html)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + html
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = None
        start, end = bounds()
        for port in range(start, end + 1):
            try:
                server = await asyncio.start_server(serve, "127.0.0.1", port, family=socket.AF_INET)
                break
            except OSError:
                pass
        assert server is not None
        try:
            report = await validate_visual(
                "visual-fixture", artifacts, f"http://127.0.0.1:{port}", "request"
            )
            assert report["viewports"], report
            assert report["passed"], report["failures"]
            assert {viewport["id"] for viewport in report["viewports"]} == {"desktop", "mobile"}
            for viewport in report["viewports"]:
                assert viewport["metrics"]["firstViewportTableCount"] > 0
                assert (tmp_path / viewport["screenshotPath"]).read_bytes().startswith(b"\x89PNG")
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
