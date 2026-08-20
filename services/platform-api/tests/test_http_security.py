import asyncio

import httpx
import pytest

from app import create_app


@pytest.mark.parametrize("path,status", [("/api/health", 200), ("/api/unknown", 404)])
def test_native_api_retains_response_protections_after_web_forwarding(path, status):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
        ) as client:
            response = await client.get(path)
            assert response.status_code == status
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"

    asyncio.run(run())


def test_hsts_remains_explicitly_configured(monkeypatch):
    from alphadeck_platform.http_security import SecurityHeaders

    monkeypatch.delenv("ALPHADECK_SECURITY_HSTS", raising=False)
    assert b"strict-transport-security" not in SecurityHeaders(None).headers
    monkeypatch.setenv("ALPHADECK_SECURITY_HSTS", "1")
    assert (
        SecurityHeaders(None).headers[b"strict-transport-security"]
        == b"max-age=31536000; includeSubDomains"
    )
