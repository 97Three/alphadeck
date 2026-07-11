from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .api_errors import ApiError
from .external_services import ExternalServices, now, validate_repository_name
from .http_input import bounded_json
from .projects import Projects


def response(value, status=200):
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "private, no-store"})


def text(body, *keys, default=None):
    return next((body[key] for key in keys if isinstance(body.get(key), str)), default)


async def payload(request):
    value = await bounded_json(request, 400000)
    if not isinstance(value, dict):
        raise ApiError(400, "INVALID_SERVICE_REQUEST", "Expected a JSON object")
    return value


def repository(body):
    name = text(body, "repo_name")
    if error := validate_repository_name(name):
        raise ApiError(400, "INVALID_REPOSITORY_NAME", error)
    return name, text(body, "description", default=""), body.get("private") is True


def external_routes(services, root, factory=None):
    router = APIRouter()

    async def service(request, action, project=None):
        database, auth = services()
        await auth.require_action(request, action, project)
        projects = Projects(database, root)
        return factory(projects) if factory else ExternalServices(projects)

    @router.get("/api/github/check-repo/{repo_name}")
    async def github_available(request: Request, repo_name: str):
        client = await service(request, "platform.tokens.manage")
        if error := validate_repository_name(repo_name):
            return response({"available": False, "error": error}, 400)
        value = await client.github_available(repo_name)
        return response(
            {"available": not value["exists"], "username": value["username"]},
            409 if value["exists"] else 200,
        )

    @router.post("/api/github/create-repo")
    async def github_create(request: Request):
        client = await service(request, "platform.tokens.manage")
        repo = await client.github_create(*repository(await payload(request)))
        user = await client.github_user()
        return response(
            {
                "success": True,
                "repo_url": repo["html_url"],
                "html_url": repo["html_url"],
                "clone_url": repo["clone_url"],
                "default_branch": repo["default_branch"],
                "owner": user["login"],
            }
        )

    @router.post("/api/projects/{project_id}/github/connect")
    async def github_connect(request: Request, project_id: str):
        client = await service(request, "project.services.manage", project_id)
        data = await client.github_connect(project_id, *repository(await payload(request)))
        return response(
            {"success": True, **data, "message": "GitHub repository created and connected"}
        )

    @router.post("/api/projects/{project_id}/github/push")
    async def github_push(request: Request, project_id: str):
        client = await service(request, "project.deploy", project_id)
        await client.github_push(project_id)
        return response({"success": True, "message": "Changes pushed to GitHub"})

    @router.get("/api/projects/{project_id}/services")
    async def project_services(request: Request, project_id: str):
        client = await service(request, "project.services.read", project_id)
        return response(
            [
                {**item, "service_data": item["serviceData"]}
                for item in await client.connections.list(project_id)
            ]
        )

    @router.delete("/api/projects/{project_id}/services/{service_id}")
    async def disconnect(request: Request, project_id: str, service_id: str):
        client = await service(request, "project.services.manage", project_id)
        if not await client.connections.delete(project_id, service_id):
            return response({"success": False, "error": "Service connection not found"}, 404)
        return response({"success": True, "message": "Service disconnected successfully"})

    @router.get("/api/supabase/organizations")
    async def supabase_organizations(request: Request):
        client = await service(request, "platform.tokens.manage")
        value = await client.fetch("supabase", "/organizations")
        return response(
            {"success": True, "organizations": value if isinstance(value, list) else []}
        )

    @router.post("/api/supabase/create-project")
    async def supabase_create(request: Request):
        body = await payload(request)
        identity = text(body, "project_id", "projectId")
        name, password = text(body, "project_name"), text(body, "db_pass")
        organization = text(body, "organization_id", "organizationId")
        if not all((identity, name, password, organization)):
            return response(
                {
                    "success": False,
                    "error": "project_id, project_name, organization_id, and db_pass are required",
                },
                400,
            )
        client = await service(request, "project.services.manage", identity)
        value = await client.supabase_create(
            identity, name, organization, password, text(body, "region", default="us-east-1")
        )
        return response(
            {
                "success": True,
                "project_id": value["id"],
                "name": value["name"],
                "organization_id": value["organization_id"],
                "status": value["status"],
                "region": value["region"],
                "created_at": value.get("inserted_at") or value.get("created_at") or now(),
            }
        )

    @router.get("/api/supabase/projects/{supabase_project_id}/api-keys")
    async def supabase_keys(request: Request, supabase_project_id: str):
        client = await service(request, "platform.tokens.manage")
        return response({"success": True, "keys": await client.supabase_keys(supabase_project_id)})

    @router.get("/api/supabase/projects/{supabase_project_id}")
    async def supabase_project(request: Request, supabase_project_id: str):
        client = await service(request, "platform.tokens.manage")
        return response(
            {
                "success": True,
                "project": await client.fetch(
                    "supabase", "/projects/" + quote(supabase_project_id, safe="")
                ),
            }
        )

    @router.post("/api/projects/{project_id}/supabase/connect")
    async def supabase_connect(request: Request, project_id: str):
        client = await service(request, "project.services.manage", project_id)
        body = await payload(request)
        identity, url = text(body, "project_id", "supabase_project_id"), text(body, "project_url")
        if not identity or not url:
            return response(
                {"success": False, "error": "project_id and project_url are required"}, 400
            )
        return response(
            {
                "success": True,
                "data": await client.supabase_connect(
                    project_id, identity, url, text(body, "project_name"), text(body, "region")
                ),
            }
        )

    @router.get("/api/vercel/check-project/{name}")
    async def vercel_available(request: Request, name: str):
        client = await service(request, "platform.tokens.manage")
        return response(
            await client.vercel_available(
                name, request.query_params.get("teamId", request.query_params.get("team_id"))
            )
        )

    @router.post("/api/projects/{project_id}/vercel/connect")
    async def vercel_connect(request: Request, project_id: str):
        client = await service(request, "project.services.manage", project_id)
        body = await payload(request)
        if not (name := text(body, "project_name")):
            return response({"success": False, "error": "project_name is required"}, 400)
        data = await client.vercel_connect(
            project_id, name, text(body, "team_id", "teamId"), text(body, "github_repo")
        )
        return response(
            {"success": True, "data": data, "message": "Connected Vercel project " + name}
        )

    @router.post("/api/projects/{project_id}/vercel/deploy")
    async def vercel_deploy(request: Request, project_id: str):
        client = await service(request, "project.deploy", project_id)
        return response(await client.vercel_deploy(project_id))

    @router.get("/api/projects/{project_id}/vercel/deployment/current")
    async def vercel_status(request: Request, project_id: str):
        client = await service(request, "project.services.read", project_id)
        return response(await client.vercel_status(project_id))

    return router
