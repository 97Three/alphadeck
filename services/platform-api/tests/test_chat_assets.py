import asyncio
import base64
import json
from uuid import uuid4

import httpx
import pytest
from test_projects import client_for, project  # noqa: F401

from alphadeck_platform.image_assets import ImageAssetError, asset_path, decode_base64, validate

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/5XcAAAAASUVORK5CYII="
)


def test_uploaded_image_is_protected_mirrored_and_bound_to_queued_research(project):  # noqa: F811
    async def run():
        async with client_for(project) as client:
            upload = await client.post(
                f"/api/assets/{project['id']}/upload",
                files={"file": ("持仓.png", PNG, "image/png")},
            )
            assert upload.status_code == 200, upload.text
            data = upload.json()
            assert (project["workspace"] / data["path"]).read_bytes() == PNG
            assert (project["workspace"] / data["publicPath"]).read_bytes() == PNG
            image = await client.get(f"/api/assets/{project['id']}/{data['filename']}")
            assert image.content == PNG and image.headers["cache-control"] == "private, no-store"
            identity = "image-request-" + uuid4().hex
            queued = await client.post(
                f"/api/chat/{project['id']}/act",
                json={
                    "requestId": identity,
                    "images": [{"path": data["path"], "name": "持仓", "mimeType": "image/png"}],
                },
            )
            assert queued.status_code == 202, queued.text
            row = await project["database"].fetch_one(
                "SELECT execution_envelope FROM agent_generation_jobs WHERE request_id=%s",
                (identity,),
            )
            payload = row["execution_envelope"]["payload"]
            assert payload["processedImages"][0]["size"] == len(PNG)
            assert payload["processedImages"][0]["publicUrl"] == data["publicUrl"]
            assert "quant_extract_uploaded_image" in payload["imageAttachmentInstruction"]
            manifest = json.loads(
                (project["workspace"] / ".data-agent/attachments.json").read_text()
            )
            assert (
                manifest["requestId"] == identity
                and manifest["attachments"][0]["path"] == data["path"]
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=project["app"]), base_url="http://test"
        ) as anonymous:
            denied = await anonymous.get(f"/api/assets/{project['id']}/{data['filename']}")
            assert denied.status_code == 401

    asyncio.run(run())


def test_act_acknowledges_durable_work_once_without_calling_model_or_market(project):  # noqa: F811
    async def run():
        async with client_for(project) as client:
            identity = "act-" + uuid4().hex
            responses = await asyncio.gather(
                *(
                    client.post(
                        f"/api/chat/{project['id']}/act",
                        json={
                            "instruction": "分析贵州茅台",
                            "requestId": identity,
                            "selectedModel": "deepseek-v4-flash",
                        },
                    )
                    for _ in range(2)
                )
            )
            assert sorted(response.status_code for response in responses) == [202, 409]
            accepted = next(
                response.json() for response in responses if response.status_code == 202
            )
            assert accepted["status"] == "queued"
            jobs = await project["database"].fetch_all(
                "SELECT * FROM agent_generation_jobs WHERE request_id=%s", (identity,)
            )
            assert len(jobs) == 1 and jobs[0]["status"] == "pending"
            assert jobs[0]["stage"] == "planning_data_prefetch"
            assert (
                jobs[0]["execution_envelope"]["payload"]["userMessageId"]
                == accepted["userMessageId"]
            )
            assert not await project["database"].fetch_one(
                "SELECT id FROM agent_runs WHERE request_id=%s", (identity,)
            )
            assert not (project["workspace"] / ".data-agent/finance-run-plan.json").exists()
            event = await project["database"].fetch_one(
                "SELECT quantity FROM usage_events WHERE source_type='user_request' AND source_id=%s AND metric='agent.requests.daily'",
                (identity,),
            )
            assert event["quantity"] == 1
            for body, status in (
                ({"instruction": "x", "legacy_field": True}, 400),
                ({"instruction": "中文" * 90000}, 413),
                ({"instruction": "x", "requestId": "../escape"}, 400),
                ({"instruction": "x", "images": [{"path": "assets/../bad.png"}]}, 400),
            ):
                response = await client.post(f"/api/chat/{project['id']}/act", json=body)
                assert response.status_code == status, response.text
            missing = await client.post(
                f"/api/chat/{project['id']}/act",
                json={"images": [{"path": "assets/not-found.png"}]},
            )
            assert missing.status_code == 404

    asyncio.run(run())


def test_image_mime_and_logo_validation_reject_invalid_source_files(project):  # noqa: F811
    async def run():
        async with client_for(project) as client:
            wrong = await client.post(
                f"/api/assets/{project['id']}/upload", files={"file": ("x.jpg", PNG, "image/jpeg")}
            )
            assert wrong.status_code == 400
            bad = await client.post(
                f"/api/assets/{project['id']}/upload",
                files={"file": ("x.png", b"<script>bad</script>", "image/png")},
            )
            assert bad.status_code == 400
            logo = await client.post(
                f"/api/assets/{project['id']}/logo",
                json={"b64_png": "data:image/png;base64," + base64.b64encode(PNG).decode()},
            )
            assert logo.json() == {"success": True, "path": "assets/logo.png"}
            assert (project["workspace"] / "assets/logo.png").read_bytes() == PNG
            link = project["workspace"] / "assets/linked.png"
            link.symlink_to(project["workspace"] / "assets/logo.png")
            denied = await client.get(f"/api/assets/{project['id']}/linked.png")
            assert denied.status_code >= 400

    asyncio.run(run())


def test_image_bytes_and_canonical_base64_limits(tmp_path):
    assert validate(PNG) == "image/png"
    assert decode_base64(base64.b64encode(PNG).decode().rstrip("=")) == PNG
    for bad in (
        "A",
        "SGVsbG8=",
        "AB==",
        "data:image/jpeg;base64," + base64.b64encode(PNG).decode(),
    ):
        with pytest.raises(ImageAssetError):
            decode_base64(bad)
    with pytest.raises(ImageAssetError):
        validate(PNG, maximum=2)
    with pytest.raises(ImageAssetError):
        asset_path(tmp_path, "../escape.png")
