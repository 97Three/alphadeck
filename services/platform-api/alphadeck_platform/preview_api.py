from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .dispatch import Dispatch
from .generation_state import GenerationState
from .generation_terminal import reconcile_dispatch, requires_acceptance, terminal_snapshot
from .mission_store import Missions
from .sessions import iso
from .validation_reports import read_report


def preview_routes(services, manager_factory):
    router = APIRouter()

    @router.post("/api/projects/{project_id}/preview/start")
    async def start(request: Request, project_id: str):
        _, auth = services()
        await auth.require_action(request, "project.update", project_id)
        manager = manager_factory()
        try:
            return {"success": True, "data": await manager.start(project_id)}
        except Exception as first:
            try:
                await manager.stop(project_id)
                return {"success": True, "recovered": True, "data": await manager.start(project_id)}
            except Exception as cause:
                return JSONResponse(
                    {
                        "success": False,
                        "error": str(cause) or "Failed to start preview",
                        "firstError": str(first),
                    },
                    status_code=500,
                )

    @router.get("/api/projects/{project_id}/preview/status")
    async def status(request: Request, project_id: str):
        _, auth = services()
        await auth.require_action(request, "project.read", project_id)
        manager = manager_factory()
        row = await manager.projects.get(project_id)
        return {
            "success": True,
            "data": await manager.reconciled(project_id, row["preview_url"], row["preview_port"]),
        }

    @router.post("/api/projects/{project_id}/preview/stop")
    async def stop(request: Request, project_id: str):
        _, auth = services()
        await auth.require_action(request, "project.update", project_id)
        manager = manager_factory()
        try:
            body = await request.json()
        except ValueError:
            body = None
        if request.headers.get("x-alphadeck-preview-intent") != "explicit-user-stop" and (
            not isinstance(body, dict) or body.get("intent") != "explicit-user-stop"
        ):
            return {"success": True, "ignored": True, "data": manager.status(project_id)}
        return {"success": True, "data": await manager.stop(project_id)}

    @router.post("/api/projects/{project_id}/install-dependencies")
    async def install(request: Request, project_id: str):
        _, auth = services()
        await auth.require_action(request, "project.source.write", project_id)
        try:
            result = await manager_factory().install_dependencies(project_id)
            return {"success": True, "logs": result["logs"]}
        except Exception as cause:
            return JSONResponse(
                {"success": False, "error": str(cause) or "Failed to install dependencies"},
                status_code=500,
            )

    @router.get("/api/projects/{project_id}/generation/status")
    async def generation_status(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.read", project_id)
        manager = manager_factory()
        row = await manager.projects.get(project_id)
        root = manager.projects.managed_path(row, must_exist=True)
        dispatch = Dispatch(database)
        await dispatch.reconcile(project=project_id)
        jobs = await dispatch.jobs(project_id, 1)
        job = jobs[0] if jobs else None
        projected = GenerationState(root).read()
        generation = reconcile_dispatch(
            projected,
            {
                "projectId": job["project_id"],
                "requestId": job["request_id"],
                "status": job["status"],
                "stage": job["stage"],
                "queuedAt": iso(job["queued_at"]),
                "cliPreference": job["cli_preference"],
                "errorMessage": job["error_message"],
            }
            if job
            else None,
        )
        preview = await manager.reconciled(project_id, row["preview_url"], row["preview_port"])
        mission = (
            await Missions(database).accepted_snapshot(project_id, generation["requestId"])
            if generation and requires_acceptance(generation)
            else None
        )
        return {
            "success": True,
            "data": terminal_snapshot(
                generation,
                read_report(root),
                preview,
                acceptedMission=mission,
                persistedPreviewUrl=row["preview_url"],
            ),
        }

    return router
