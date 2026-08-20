import asyncio

import httpx
from test_projects import client_for, project  # noqa: F401


def test_python_page_admission_membership_password_change_and_browser_origin(project):  # noqa: F811
    async def run():
        async with client_for(project) as client:

            async def decision(path, method="GET", **kwargs):
                response = await client.get(
                    "/internal/frontend/access", params={"path": path, "method": method}, **kwargs
                )
                assert (
                    response.status_code == 200
                    and response.headers["cache-control"] == "private, no-store"
                )
                return response.json()

            assert (await decision("/login")) == {"kind": "redirect", "location": "/"}
            assert (await decision("/"))["kind"] == "allow"
            assert (await decision("/" + project["id"] + "/chat"))["kind"] == "allow"
            assert (await decision("/unknown-project/chat")) == {
                "kind": "rewrite",
                "location": "/forbidden",
                "status": 404,
            }
            assert (await decision("/admin/users"))["status"] == 403
            assert (await decision("/api/admin/users"))["kind"] == "deny"
            assert (await decision("/api/projects", "POST"))["status"] == 403
            assert (
                await decision(
                    "/api/projects", "POST", headers={"X-AlphaDeck-Request": "same-origin"}
                )
            )["kind"] == "allow"
            assert (await decision("/api/projects", "POST", headers={"Origin": "http://test"}))[
                "kind"
            ] == "allow"
            assert (
                await decision(
                    "/api/projects", "POST", headers={"Origin": "https://foreign.invalid"}
                )
            )["status"] == 403
            denied = await client.put(
                "/api/projects/" + project["id"],
                json={"name": "forged"},
                headers={"Origin": "https://foreign.invalid"},
            )
            assert denied.status_code == 403
            async with project["database"].transaction() as tx:
                await tx.execute(
                    "UPDATE auth_users SET must_change_password=true WHERE id=(SELECT owner_id FROM projects WHERE id=%s)",
                    (project["id"],),
                )
            assert (await decision("/strategy-platform"))[
                "location"
            ] == "/account/security?required=1"
            assert (await decision("/account/security"))["kind"] == "allow"
            assert (await decision("/api/auth/get-session"))["kind"] == "allow"
            assert (await decision("/api/projects"))["status"] == 428
            assert (await decision("/api/health"))["kind"] == "allow"
            assert (await decision("//foreign.invalid"))["status"] == 400
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=project["app"]), base_url="http://test"
        ) as anonymous:
            assert (
                await anonymous.get(
                    "/internal/frontend/access", params={"path": "/strategy-platform"}
                )
            ).json() == {"kind": "redirect", "location": "/login", "next": "/strategy-platform"}
            assert (
                await anonymous.get("/internal/frontend/access", params={"path": "/api/projects"})
            ).json()["status"] == 401
            assert (
                await anonymous.get("/internal/frontend/access", params={"path": "/login"})
            ).json()["kind"] == "allow"

    asyncio.run(run())
