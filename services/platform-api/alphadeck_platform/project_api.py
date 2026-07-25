import logging
import re
from datetime import UTC
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from .files import FileBrowser, FileBrowserError, assert_allowed, safe_path
from .projects import ProjectProvisioningUncertain, Projects, serialize_project
from .quota import Quota, QuotaError
from .skill_integrity import read_regular


class ProjectRequestError(Exception):
    def __init__(self, issues, creation=False):
        self.issues = issues
        self.creation = creation


def validate_create(body):
    allowed = {
        "projectId",
        "name",
        "initialPrompt",
        "selectedModel",
        "description",
        "capabilityId",
        "capabilitySelectionSource",
        "agentProfileId",
    }
    if not isinstance(body, dict) or body.keys() - allowed:
        raise ProjectRequestError([], True)
    try:
        shared = validate_update(
            {key: body[key] for key in ("name", "description", "selectedModel") if key in body}
            | {"name": body.get("name")}
        )
        result = {**body, **shared}
        identity = body.get("projectId")
        if not isinstance(identity, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", identity.strip()
        ):
            raise ValueError("Invalid project ID")
        result["projectId"] = identity.strip()
        for field, maximum in (
            ("initialPrompt", 200000),
            ("capabilityId", 128),
            ("agentProfileId", 128),
        ):
            if field not in result:
                continue
            if not isinstance(result[field], str):
                raise ValueError("Expected string")
            value = result[field].strip()
            if len(value.encode("utf-16-le", errors="surrogatepass")) // 2 > maximum or (
                field != "initialPrompt" and not value
            ):
                raise ValueError("Invalid field length")
            result[field] = value
        if "agentProfileId" in result and not re.fullmatch(
            r"[a-z][a-z0-9._-]{1,127}", result["agentProfileId"]
        ):
            raise ValueError("Invalid agent profile ID")
        if "capabilitySelectionSource" in result and result["capabilitySelectionSource"] not in (
            "manual",
            "default",
            "inferred",
        ):
            raise ValueError("Invalid capability selection source")
        return result
    except (ValueError, ProjectRequestError) as error:
        raise ProjectRequestError([], True) from error


def validate_update(body):
    fields = {
        "name",
        "description",
        "status",
        "previewUrl",
        "previewPort",
        "preferredCli",
        "selectedModel",
        "settings",
    }
    issues = []
    if not isinstance(body, dict) or not body:
        raise ProjectRequestError(
            [{"path": "$", "message": "At least one project field is required."}]
        )
    for field in body.keys() - fields:
        issues.append({"path": "$", "message": f"Unrecognized key: {field}"})
    result = {}
    for field, value in body.items():
        message = None
        if field in {"name", "description", "previewUrl", "selectedModel", "settings"}:
            nullable = field in {"description", "previewUrl"}
            if value is None and nullable:
                result[field] = None
                continue
            if not isinstance(value, str):
                message = "Expected string"
            else:
                if field != "settings":
                    value = value.strip()
                maximum = {
                    "name": 120,
                    "description": 20000,
                    "previewUrl": 2048,
                    "selectedModel": 256,
                    "settings": 1000000,
                }[field]
                length = len(value.encode("utf-16-le", errors="surrogatepass")) // 2
                if length > maximum or (not length and field in {"name", "selectedModel"}):
                    message = f"Invalid {field} length"
        elif field == "status":
            if value not in (
                "idle",
                "running",
                "stopped",
                "error",
                "initializing",
                "active",
                "failed",
            ):
                message = "Invalid project status"
        elif field == "preferredCli":
            if value != "pi":
                message = "Expected pi"
        elif field == "previewPort":
            if value is not None:
                if (
                    type(value) not in {int, float}
                    or not 1 <= value <= 65535
                    or int(value) != value
                ):
                    message = "Expected an integer between 1 and 65535"
                else:
                    value = int(value)
        if message:
            issues.append({"path": field, "message": message})
        elif field in fields:
            result[field] = value
    if issues:
        raise ProjectRequestError(issues)
    return result


def file_response_error(request, message, status):
    payload = {"error": message}
    if not request.url.path.startswith("/api/repo/"):
        payload["success"] = False
    return JSONResponse(payload, status_code=status)


def project_routes(services, root, *, lock_wait_seconds=5, provisioner=None):
    router = APIRouter()

    def store():
        database, auth = services()
        projects = Projects(database, root, provisioner)
        return projects, auth, FileBrowser(projects, lock_wait_seconds=lock_wait_seconds)

    @router.get("/api/projects")
    async def list_projects(request: Request):
        projects, auth, _ = store()
        user = await auth.require_project_list(request)
        return {"success": True, "data": await projects.list(user)}

    @router.post("/api/projects")
    async def create_project(request: Request):
        projects, auth, _ = store()
        context = await auth.require_action(request, "project.create")
        try:
            body = await request.json()
        except ValueError:
            body = None
        fields = validate_create(body)
        quota = Quota(projects.database)
        reservation_id = None
        created = False
        uncertain = False
        try:
            if context.user:
                operation = request.headers.get("idempotency-key", "").strip()
                if not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", operation):
                    operation = str(uuid4())
                result = await quota.reserve(
                    context.actor_user_id,
                    "projects.owned",
                    1,
                    f"project-create:{context.actor_user_id}:{operation}",
                    ttl=3600,
                )
                reservation = result["reservation"]
                if reservation["status"] != "active" or reservation["idempotent"]:
                    return JSONResponse(
                        {
                            "success": False,
                            "error": "PROJECT_CREATE_OPERATION_ALREADY_USED",
                            "message": "该项目创建操作正在执行或已经结束；请读取原操作结果，重新创建时使用新的幂等键。",
                        },
                        status_code=409,
                    )
                reservation_id = reservation["id"]
            project = await projects.create(fields, context.actor_user_id if context.user else None)
            created = True
            if reservation_id:
                try:
                    created_ms = int(project["created_at"].replace(tzinfo=UTC).timestamp() * 1000)
                    await quota.settle(
                        reservation_id,
                        1,
                        "project",
                        source_id=project["id"],
                        event_key=f"project:{project['id']}:{created_ms}:owned",
                    )
                except Exception:
                    logging.exception(
                        "Created project has a pending idempotent ownership settlement"
                    )
            if context.user:
                try:
                    async with projects.database.transaction() as connection:
                        await connection.execute(
                            "INSERT INTO auth_audit_events (id,actor_user_id,event_type,target_type,target_id,outcome,metadata) VALUES (%s,%s,'project.created','project',%s,'success',%s)",
                            (str(uuid4()), context.actor_user_id, project["id"], Jsonb({})),
                        )
                except Exception:
                    logging.exception("Project creation audit recording needs retry")
            return JSONResponse(
                {"success": True, "data": serialize_project(project)}, status_code=201
            )
        except ProjectProvisioningUncertain:
            uncertain = True
            return JSONResponse(
                {
                    "success": False,
                    "error": "PROJECT_INITIALIZATION_PENDING",
                    "message": "项目初始化状态尚未确认，请稍后查询项目状态。",
                },
                status_code=503,
            )
        except (UniqueViolation, FileExistsError):
            return JSONResponse(
                {
                    "success": False,
                    "error": "Failed to create project",
                    "message": "Resource already exists",
                },
                status_code=409,
            )
        except QuotaError:
            raise
        except Exception:
            logging.exception("Failed to create project")
            return JSONResponse(
                {
                    "success": False,
                    "error": "Failed to create project",
                    "message": "Internal server error",
                },
                status_code=500,
            )
        finally:
            if reservation_id and not created and not uncertain:
                try:
                    await quota.release(reservation_id)
                except Exception:
                    logging.exception("Project creation reservation release needs retry")

    @router.get("/api/projects/{project_id}")
    async def get_project(request: Request, project_id: str):
        projects, auth, _ = store()
        await auth.require_action(request, "project.read", project_id)
        return {"success": True, "data": serialize_project(await projects.get(project_id))}

    @router.get("/api/projects/{project_id}/artifact")
    async def artifact(request: Request, project_id: str):
        projects, auth, _ = store()
        await auth.require_action(request, "project.source.read", project_id)
        row = await projects.get(project_id)
        relative = request.query_params.get("path")
        if not relative:
            return JSONResponse(
                {"success": False, "error": "path query parameter is required"}, status_code=400
            )
        assert_allowed(relative)
        root = projects.managed_path(row, must_exist=True)
        target = safe_path(root, relative.replace("\\", "/").lstrip("/"))
        try:
            content = read_regular(target, 64 * 1024 * 1024)
        except FileNotFoundError:
            raise FileBrowserError("Artifact not found", 404) from None
        except ValueError:
            raise FileBrowserError("Artifact not found", 404) from None
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".json": "application/json; charset=utf-8",
        }.get(target.suffix.lower(), "text/plain; charset=utf-8")
        return Response(content, media_type=mime, headers={"Cache-Control": "no-store"})

    @router.put("/api/projects/{project_id}")
    async def update_project(request: Request, project_id: str):
        projects, auth, _ = store()
        await auth.require_action(request, "project.update", project_id)
        try:
            body = await request.json()
        except ValueError:
            body = None
        return {
            "success": True,
            "data": serialize_project(await projects.update(project_id, validate_update(body))),
        }

    @router.delete("/api/projects/{project_id}")
    async def delete_project(request: Request, project_id: str):
        projects, auth, _ = store()
        context = await auth.require_action(request, "project.delete", project_id)
        try:
            deleted = await projects.delete(
                project_id, context.actor_user_id if context.user else None
            )
        except ValueError as error:
            return JSONResponse(
                {"success": False, "error": "Failed to delete project", "message": str(error)},
                status_code=500,
            )
        if not deleted:
            return JSONResponse({"success": False, "error": "Project not found"}, status_code=404)
        return {"success": True, "message": "Project deleted successfully"}

    @router.get("/api/repo/{project_id}/tree")
    @router.get("/api/projects/{project_id}/files")
    async def tree(request: Request, project_id: str):
        _, auth, browser = store()
        await auth.require_action(request, "project.source.read", project_id)
        repo = request.url.path.startswith("/api/repo/")
        directory = request.query_params.get("dir" if repo else "path", ".")
        entries = await browser.list(project_id, directory)
        if repo:
            payload = [
                {
                    "path": entry["path"],
                    "type": "dir" if entry["type"] == "directory" else "file",
                    "hasChildren": entry["hasChildren"],
                    **({"size": entry["size"]} if "size" in entry else {}),
                }
                for entry in entries
            ]
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
        return {"success": True, "data": {"entries": entries}}

    @router.get("/api/repo/{project_id}/file")
    @router.get("/api/projects/{project_id}/files/content")
    async def read_file(request: Request, project_id: str):
        _, auth, browser = store()
        await auth.require_action(request, "project.source.read", project_id)
        path = request.query_params.get("path")
        if not path:
            return file_response_error(request, "path query parameter is required", 400)
        file = await browser.read(project_id, path)
        if request.url.path.startswith("/api/repo/"):
            return JSONResponse(file, headers={"Cache-Control": "no-store"})
        return {"success": True, "data": file}

    @router.put("/api/repo/{project_id}/file")
    @router.put("/api/projects/{project_id}/files/content")
    async def write_file(request: Request, project_id: str):
        _, auth, browser = store()
        await auth.require_action(request, "project.source.write", project_id)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("path"), str) or not body["path"]:
            return file_response_error(request, "path is required", 400)
        if not isinstance(body.get("content"), str):
            return file_response_error(request, "content must be a string", 400)
        await browser.write(project_id, body["path"], body["content"])
        if request.url.path.startswith("/api/repo/"):
            return {"success": True, "path": body["path"]}
        return {"success": True, "data": {"path": body["path"]}}

    return router
