import asyncio
import json
from pathlib import Path

import httpx
import pytest

from alphadeck_platform import research_notifications
from alphadeck_platform.research_report_content import build_report

ROOT = Path(__file__).parent / "fixtures"
REPORTS = json.loads((ROOT / "research-reports.json").read_text())
NOTIFICATIONS = json.loads((ROOT / "research-notifications.json").read_text())


@pytest.mark.parametrize("case", REPORTS)
def test_research_report_content_and_evidence_score_match_original(case):
    assert build_report(*case["args"], date="2026/11/03") == case["expected"]


@pytest.mark.parametrize("case", NOTIFICATIONS)
def test_notification_adapters_match_original_without_contacting_real_recipients(case, monkeypatch):
    monkeypatch.setattr(research_notifications, "now", lambda: "2026-11-03T00:00:00.000Z")
    expected = case["expected"]

    def handler(request):
        assert request.url.host == "webhook.example.test"
        value = expected["payload"].get("response") or {}
        return httpx.Response(
            503 if (expected.get("error") or "").startswith("HTTP 503") else 200,
            content=json.dumps(value, separators=(",", ":")),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            value = await research_notifications.deliver_notification(
                case["channel"], case["report"], client=client, env={}
            )
        assert value == expected

    asyncio.run(run())
