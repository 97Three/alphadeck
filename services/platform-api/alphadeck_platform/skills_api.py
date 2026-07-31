import inspect
import re
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import Response

from .api_errors import ApiError
from .config import ROOT
from .finance_values import js_string
from .http_input import bounded_body, bounded_json
from .messages import MessageResponse
from .privileged_request import authorized_skill_mutation
from .projects import Projects
from .skill_administration import SkillsAdministration
from .skill_catalog import TARGETS, valid_skill
from .skill_dashboard import active_dashboard
from .skill_deployment import SkillConflict, deploy_skills
from .skill_market import market, verified_package


def result(value, status=200):
    return MessageResponse(
        value, status_code=status, headers={"Cache-Control": "private, no-store"}
    )


def string(value):
    return js_string(value) if value is not None else ""


async def invoke(operation, *, failure_status=400):
    try:
        value = operation()
        value = await value if inspect.isawaitable(value) else value
        return result({"success": True, "data": value})
    except SkillConflict as cause:
        return result({"success": False, "error": str(cause)}, 409)
    except (ValueError, OSError, KeyError, TypeError) as cause:
        return result({"success": False, "error": str(cause)}, failure_status)


def installation_request(value):
    if (
        not isinstance(value, dict)
        or not {"projectId", "target", "action", "expectedRevision"} <= value.keys()
        or set(value)
        - {
            "projectId",
            "target",
            "action",
            "skillId",
            "version",
            "expectedRevision",
            "overwriteModified",
        }
    ):
        raise ValueError("安装请求无效。")
    if (
        not isinstance(value["projectId"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value["projectId"])
        or value["target"] not in TARGETS
        or value["action"] not in ("install", "uninstall", "rollback")
    ):
        raise ValueError("安装请求无效。")
    if "skillId" in value and not valid_skill(value["skillId"]):
        raise ValueError("技能 ID 无效。")
    if "version" in value and (
        not isinstance(value["version"], str)
        or len(value["version"]) > 32
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["version"])
    ):
        raise ValueError("版本号无效。")
    if value["expectedRevision"] is not None:
        UUID(value["expectedRevision"])
    if "overwriteModified" in value and type(value["overwriteModified"]) is not bool:
        raise ValueError("overwriteModified 必须是布尔值。")
    return value


def skills_routes(services, projects_root, *, catalogue_root=ROOT, administration_factory=None):
    router = APIRouter()
    catalogue_root = Path(catalogue_root)

    def administration():
        return (
            administration_factory(catalogue_root)
            if administration_factory
            else SkillsAdministration(catalogue_root)
        )

    @router.get("/api/skills")
    async def skills(request: Request):
        _, auth = services()
        await auth.require_action(request, "quant.data.read")
        studio = request.query_params.get("view") == "studio"
        if studio:
            await auth.require_action(request, "platform.settings.manage")
        return await invoke(
            lambda: administration().studio() if studio else active_dashboard(catalogue_root),
            failure_status=500,
        )

    @router.post("/api/skills")
    async def mutate(request: Request):
        _, auth = services()
        context = await auth.require_action(request, "platform.settings.manage")
        authorized_skill_mutation(request, context)
        admin = administration()
        if "multipart/form-data" in request.headers.get("content-type", ""):
            await bounded_body(request, 7 * 1024 * 1024)
            form = await request.form(max_files=1, max_fields=5, max_part_size=6 * 1024 * 1024)
            try:
                file = form.get("file")
                if form.get("action") != "upload-package" or not isinstance(file, UploadFile):
                    # Starlette and FastAPI share the upload protocol but not their subclass identity.
                    from starlette.datastructures import UploadFile as StarletteUpload

                    if form.get("action") != "upload-package" or not isinstance(
                        file, StarletteUpload
                    ):
                        raise ApiError(
                            400, "INVALID_SKILL_UPLOAD", "缺少技能上传文件或 action 无效。"
                        )
                content = await file.read(5242881)
                return await invoke(
                    lambda: admin.upload(
                        string(form.get("skillId")),
                        content,
                        file.filename or "",
                        expected=string(form.get("expectedRevision")),
                        actor=context.actor_user_id,
                    )
                )
            finally:
                await form.close()
        body = await bounded_json(request, 2 * 1024 * 1024)
        if not isinstance(body, dict):
            raise ApiError(400, "INVALID_SKILL_REQUEST", "技能请求必须为 JSON 对象。")
        action, identity = string(body.get("action")), string(body.get("skillId"))
        mutation = {
            "expected": string(body.get("expectedRevision")),
            "actor": context.actor_user_id,
        }
        if action in ("read-source", "read-file"):
            return await invoke(
                lambda: admin.read_file(identity, string(body.get("filePath")) or "SKILL.md")
            )
        if action in ("save-source", "save-file"):
            path = (
                "SKILL.md"
                if action == "save-source"
                else string(body.get("filePath")) or "SKILL.md"
            )
            content = (
                body.get("skillMd", body.get("content"))
                if action == "save-source"
                else body.get("content")
            )
            return await invoke(
                lambda: admin.save_file(identity, path, string(content), **mutation)
            )
        if action == "discard-draft":
            return await invoke(lambda: admin.discard(identity, **mutation))
        if action in ("delete-file", "create-folder", "delete-folder"):
            path = string(body.get("filePath" if action == "delete-file" else "folderPath"))
            method = {
                "delete-file": "deleteSkillFile",
                "create-folder": "createSkillFolder",
                "delete-folder": "deleteSkillFolder",
            }[action]
            return await invoke(lambda: admin.file_mutation(identity, path, method, **mutation))
        if action == "diff-version":
            return await invoke(lambda: admin.diff(identity))
        if action == "publish-version":
            value = body.get("changes")
            changes = (
                [string(item) for item in value]
                if isinstance(value, list)
                else [
                    line
                    for line in (
                        re.sub(r"^[-*]\s*", "", line).strip() for line in value.split("\n")
                    )
                    if line
                ]
                if isinstance(value, str)
                else []
            )
            return await invoke(
                lambda: admin.publish(
                    identity,
                    string(body.get("version")),
                    string(body.get("summary")),
                    changes,
                    **mutation,
                )
            )
        if action == "rollback-version":
            return await invoke(
                lambda: admin.rollback(identity, string(body.get("version")), **mutation)
            )
        return result({"success": False, "error": "不支持的 action。"}, 400)

    @router.get("/api/skills/market")
    async def skills_market(request: Request):
        database, auth = services()
        await auth.require_action(request, "quant.data.read")
        identity, target = (
            request.query_params.get("projectId"),
            request.query_params.get("target", "pi-agent"),
        )
        if target not in TARGETS:
            return result({"success": False, "error": "不支持的 Agent。"}, 400)
        project = None
        if identity is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identity):
                return result({"success": False, "error": "项目 ID 无效。"}, 400)
            await auth.require_action(request, "project.read", identity)
            projects = Projects(database, projects_root)
            project = {
                "id": identity,
                "workspace": projects.managed_path(await projects.get(identity), must_exist=True),
                "target": target,
            }
        return await invoke(lambda: market(catalogue_root, project), failure_status=503)

    @router.get("/api/skills/{skillId}/package")
    async def package(request: Request, skillId: str):
        skill_id = skillId
        _, auth = services()
        await auth.require_action(request, "quant.data.read")
        if not valid_skill(skill_id):
            return result({"success": False, "error": "技能 ID 无效。"}, 400)
        try:
            artifact = verified_package(catalogue_root, skill_id)
        except (ValueError, OSError):
            return result(
                {"success": False, "error": "技能未发布或完整性校验失败，暂不可下载。"}, 409
            )
        return Response(
            artifact["content"],
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{skill_id}-{artifact["version"]}.tgz"',
                "Cache-Control": "private, no-store",
                "X-Skill-Version": artifact["version"],
                "X-Content-SHA256": artifact["sha256"],
            },
        )

    @router.post("/api/skills/installations")
    async def install(request: Request):
        try:
            body = installation_request(await bounded_json(request, 65536))
        except (ValueError, TypeError, AttributeError):
            return result({"success": False, "error": "安装请求无效。"}, 400)
        database, auth = services()
        context = await auth.require_action(request, "project.update", body["projectId"])
        await auth.require_action(request, "quant.data.read")
        authorized_skill_mutation(request, context)
        projects = Projects(database, projects_root)
        workspace = projects.managed_path(await projects.get(body["projectId"]), must_exist=True)
        return await invoke(
            lambda: deploy_skills(
                workspace,
                target=body["target"],
                action=body["action"],
                skill_id=body.get("skillId"),
                version=body.get("version"),
                expected_revision=body["expectedRevision"],
                overwrite_modified=body.get("overwriteModified", False),
                actor=context.actor_user_id,
                root=catalogue_root,
            )
        )

    return router
