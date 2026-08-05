import asyncio

import httpx

from app import app


async def check():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "service": "alphadeck-web"}
        assert response.headers["Cache-Control"] == "no-store, max-age=0"


if __name__ == "__main__":
    asyncio.run(check())
    print("health contract passed")
