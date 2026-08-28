import asyncio
import hashlib
import json
from pathlib import Path

from test_projects import client_for, project  # noqa: F401
from test_skill_administration import fixture_validator, skills_root  # noqa: F401

from alphadeck_platform.auth import Authorization
from alphadeck_platform.skill_administration import SkillsAdministration
from alphadeck_platform.skill_market import market
from app import create_app


def configure(project, skills_root):  # noqa: F811
    database = project["database"]
    project["app"] = create_app(
        database,
        Authorization(database, enabled=True, secret="test-secret"),
        projects_dir=project["root"],
        skills_root=skills_root,
        skills_administration_factory=lambda root: SkillsAdministration(
            root, validator=fixture_validator
        ),
    )


def test_skill_market_matches_original_reference(skills_root):  # noqa: F811
    expected = json.loads((Path(__file__).parent / "fixtures/skills-market.json").read_text())
    value = market(skills_root)
    del value["generatedAt"]
    assert value["targets"] == expected["targets"]
    assert [item["id"] for item in value["skills"]] == [item["id"] for item in expected["skills"]]
    assert value["capabilities"] == expected["capabilities"]
    assert all(item["packageVerified"] and item["health"] == "ok" for item in value["skills"])
    for item in value["skills"]:
        if item["id"] in {"platform-ui-product-design", "dashboard-visualization"}:
            assert "Next.js" not in item["boundary"]
            assert all("app/page.tsx" not in output for output in item["outputs"])


def test_native_skills_management_api_protects_drafts_and_publishes_atomically(
    project,  # noqa: F811
    actor,
    skills_root,  # noqa: F811
):  # noqa: F811
    configure(project, skills_root)

    async def run():
        async with project["database"].transaction() as tx:
            await tx.execute("UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],))
        async with client_for(project) as client:
            headers = {"Origin": "http://test"}
            studio = await client.get("/api/skills?view=studio")
            assert studio.status_code == 200
            assert len(studio.json()["data"]["skills"]) == 12
            source_body = {"action": "read-source", "skillId": "query-rewrite"}
            assert (await client.post("/api/skills", json=source_body)).status_code == 403
            source = (await client.post("/api/skills", headers=headers, json=source_body)).json()[
                "data"
            ]
            save_body = {
                "action": "save-source",
                "skillId": "query-rewrite",
                "expectedRevision": source["revision"],
                "skillMd": source["content"] + "\n# API fixture change\n",
            }
            saved = await client.post("/api/skills", headers=headers, json=save_body)
            assert saved.status_code == 200, saved.text
            assert saved.json()["data"]["draft"]
            conflict = await client.post("/api/skills", headers=headers, json=save_body)
            assert conflict.status_code == 409
            diff = (
                await client.post(
                    "/api/skills",
                    headers=headers,
                    json={"action": "diff-version", "skillId": "query-rewrite"},
                )
            ).json()["data"]
            assert diff["changed"]
            published = await client.post(
                "/api/skills",
                headers=headers,
                json={
                    "action": "publish-version",
                    "skillId": "query-rewrite",
                    "expectedRevision": saved.json()["data"]["revision"],
                    "version": "999.0.0",
                    "summary": "Test publication",
                    "changes": "- Improve instructions\n- Verify source",
                },
            )
            assert published.status_code == 200, published.text
            package = await client.get("/api/skills/query-rewrite/package")
            assert package.status_code == 200
            assert package.headers["x-skill-version"] == "999.0.0"
            assert (
                package.headers["x-content-sha256"]
                == "sha256:" + hashlib.sha256(package.content).hexdigest()
            )
            assert package.headers["content-type"] == "application/gzip"
            assert (await client.get("/api/skills/invalid.name/package")).status_code == 400
            assert (
                await client.post(
                    "/api/skills", headers={"Origin": "https://evil.test"}, json=source_body
                )
            ).status_code == 403

    asyncio.run(run())


def test_skill_installation_market_tracks_pinned_revision_and_detects_modified_files(
    project,  # noqa: F811
    actor,
    skills_root,  # noqa: F811
):  # noqa: F811
    configure(project, skills_root)

    async def run():
        async with client_for(project) as client:
            headers = {"Origin": "http://test"}
            initial = await client.get("/api/skills/market", params={"projectId": project["id"]})
            assert initial.status_code == 200, initial.text
            assert initial.json()["data"]["project"]["execution"] == "platform-current"
            installed = await client.post(
                "/api/skills/installations",
                headers=headers,
                json={
                    "projectId": project["id"],
                    "target": "pi-agent",
                    "action": "install",
                    "skillId": "query-rewrite",
                    "expectedRevision": None,
                },
            )
            assert installed.status_code == 200, installed.text
            deployment = installed.json()["data"]
            view = (
                await client.get("/api/skills/market", params={"projectId": project["id"]})
            ).json()["data"]["project"]
            assert view["execution"] == "pinned" and view["deployment"]["id"] == deployment["id"]
            assert (
                next(item for item in view["skills"] if item["skillId"] == "query-rewrite")["state"]
                == "current"
            )
            target = project["workspace"] / ".pi/skills/query-rewrite/SKILL.md"
            target.write_text(target.read_text() + "\n# Local edit\n")
            changed = (
                await client.get("/api/skills/market", params={"projectId": project["id"]})
            ).json()["data"]["project"]
            assert (
                next(item for item in changed["skills"] if item["skillId"] == "query-rewrite")[
                    "state"
                ]
                == "modified"
            )
            denied = await client.post(
                "/api/skills/installations",
                headers=headers,
                json={
                    "projectId": project["id"],
                    "target": "pi-agent",
                    "action": "uninstall",
                    "skillId": "query-rewrite",
                    "expectedRevision": None,
                },
            )
            assert denied.status_code == 409

    asyncio.run(run())
