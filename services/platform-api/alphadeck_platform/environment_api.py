from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .environment import Environment, parse_write
from .http_input import bounded_json
from .projects import Projects


def environment_routes(services, root):
    router = APIRouter()

    async def service(request, project, *, write=False):
        database, auth = services()
        await auth.require_action(
            request, "project.secrets.write" if write else "project.secrets.read", project
        )
        return Environment(Projects(database, root))

    @router.get("/api/env/{project_id}")
    async def list_variables(request: Request, project_id: str):
        env = await service(request, project_id)
        return JSONResponse(
            await env.list(project_id), headers={"Cache-Control": "private, no-store"}
        )

    @router.post("/api/env/{project_id}")
    async def create(request: Request, project_id: str):
        env = await service(request, project_id, write=True)
        value = parse_write(await bounded_json(request, 6000000))
        return JSONResponse(
            {"success": True, "data": await env.write(project_id, value)}, status_code=201
        )

    @router.post("/api/env/{project_id}/upsert")
    async def upsert(request: Request, project_id: str):
        env = await service(request, project_id, write=True)
        return {
            "success": True,
            "data": await env.write(
                project_id, parse_write(await bounded_json(request, 6000000)), upsert=True
            ),
        }

    @router.get("/api/env/{project_id}/conflicts")
    async def conflicts(request: Request, project_id: str):
        env = await service(request, project_id)
        return JSONResponse(
            await env.conflicts(project_id), headers={"Cache-Control": "private, no-store"}
        )

    @router.post("/api/env/{project_id}/sync/db-to-file")
    async def to_file(request: Request, project_id: str):
        env = await service(request, project_id, write=True)
        count = await env.synchronize(project_id)
        return {
            "success": True,
            "synced_count": count,
            "message": f"Synced {count} env vars from database to file",
        }

    @router.post("/api/env/{project_id}/sync/file-to-db")
    async def from_file(request: Request, project_id: str):
        env = await service(request, project_id, write=True)
        count = await env.synchronize(project_id, from_file=True)
        return {
            "success": True,
            "synced_count": count,
            "message": f"Synced {count} env vars from file to database",
        }

    @router.put("/api/env/{project_id}/{key}")
    async def update(request: Request, project_id: str, key: str):
        env = await service(request, project_id, write=True)
        value = parse_write(await bounded_json(request, 6000000), update=True)
        if not await env.update(project_id, key, value["value"]):
            return JSONResponse(
                {"success": False, "error": "Environment variable not found"}, status_code=404
            )
        return {"success": True, "message": f"Environment variable '{key}' updated"}

    @router.delete("/api/env/{project_id}/{key}")
    async def delete(request: Request, project_id: str, key: str):
        env = await service(request, project_id, write=True)
        if not await env.update(project_id, key, delete=True):
            return JSONResponse(
                {"success": False, "error": "Environment variable not found"}, status_code=404
            )
        return {"success": True, "message": f"Environment variable '{key}' deleted"}

    return router
