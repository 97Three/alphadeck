import asyncio
import json
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .auth import AuthorizationError
from .config import ROOT
from .generation_trace import generation_dashboard
from .projects import Projects
from .readiness import web_readiness
from .service_catalog import dependency_edges, mask_endpoint, resolved_catalog, validate_catalog
from .workspace_health import workspace_dashboard


async def compose_status():
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "ps",
            "timescaledb",
            "--format",
            "json",
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), 3)
        if process.returncode:
            raise ValueError
        rows = [json.loads(line) for line in stdout.decode().splitlines() if line.strip()]
        entry = rows[0] if rows else None
        return {
            "available": True,
            "running": bool(entry and entry.get("State", "").lower() == "running"),
            "service": {
                "name": entry.get("Name", ""),
                "state": entry.get("State", ""),
                "status": entry.get("Status", ""),
                "image": entry.get("Image", ""),
            }
            if entry
            else None,
        }
    except Exception:
        return {
            "available": False,
            "running": False,
            "service": None,
            "error": "Docker Compose status unavailable",
        }
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()


async def infrastructure_health(database, *, docker=None):
    url = os.getenv("DATABASE_URL", "")
    data = {
        "provider": "postgresql"
        if url.startswith(("postgresql://", "postgres://"))
        else "unsupported",
        "databaseUrl": mask_endpoint(url),
        "connected": False,
        "timescale": {"enabled": False, "version": None},
        "quantSchema": {"tables": []},
        "docker": docker if docker is not None else await compose_status(),
        "commands": {
            "start": "npm run db:up",
            "sync": "npm run db:migrate:python",
            "inspect": "npm run db:doctor",
            "psql": "npm run db:psql",
        },
    }
    try:
        await database.fetch_one("SELECT id FROM projects LIMIT 1")
        extension = await database.fetch_one(
            "SELECT extversion FROM pg_extension WHERE extname='timescaledb'"
        )
        tables = await database.fetch_all(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='quant' ORDER BY table_name"
        )
        data.update(
            connected=True,
            timescale={
                "enabled": extension is not None,
                "version": extension["extversion"] if extension else None,
            },
            quantSchema={"tables": [row["table_name"] for row in tables]},
        )
        return {"success": True, "data": data}, 200
    except Exception:
        return {"success": False, "data": data, "error": "Database inspection unavailable"}, 503


def operations_routes(
    services, root, *, readiness_factory=None, market_client=None, skills_root=ROOT, scans_root=None
):
    router = APIRouter()

    @router.get("/api/ops/platform")
    async def platform(request: Request):
        from .operations_platform import operations_dashboard

        database, auth = services()
        await auth.require_action(request, "platform.observability.read")
        value = await operations_dashboard(
            Projects(database, root),
            client=market_client,
            skills_root=skills_root,
            scans_root=scans_root,
            include_logs=request.query_params.get("includeLogs") == "1",
        )
        return JSONResponse(
            {"success": True, "data": value}, headers={"Cache-Control": "private, no-store"}
        )

    @router.get("/api/ready")
    async def ready():
        database, _ = services()
        value = (
            await readiness_factory(database, root)
            if readiness_factory
            else await web_readiness(database, root)
        )
        return JSONResponse(
            value,
            status_code=200 if value["ok"] else 503,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @router.get("/api/infrastructure/service-catalog")
    async def catalog(request: Request):
        _, auth = services()
        await auth.require_action(request, "platform.observability.read")
        entries, validation = resolved_catalog(), validate_catalog()
        return JSONResponse(
            {
                "success": validation["ok"],
                "data": {
                    "version": 1,
                    "services": entries,
                    "dependencies": dependency_edges(entries),
                    "validation": validation,
                },
                "error": validation["errors"][0] if validation["errors"] else None,
            },
            status_code=200 if validation["ok"] else 500,
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/infrastructure/health")
    async def infrastructure(request: Request):
        database, auth = services()
        await auth.require_action(request, "platform.observability.read")
        value, status = await infrastructure_health(database)
        return JSONResponse(
            value, status_code=status, headers={"Cache-Control": "private, no-store"}
        )

    @router.get("/api/workspaces/health")
    async def workspaces(request: Request):
        database, auth = services()
        await auth.require_action(request, "platform.observability.read")
        return JSONResponse(
            {"success": True, "data": await workspace_dashboard(Projects(database, root))},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/workspaces/trace")
    async def trace(request: Request):
        database, auth = services()
        await auth.require_action(request, "platform.observability.read")
        match = re.match(r"^[+-]?[0-9]+", request.query_params.get("events", "").lstrip())
        limit = int(match[0]) if match else 220
        return JSONResponse(
            {
                "success": True,
                "data": await generation_dashboard(
                    Projects(database, root),
                    summary_only=request.query_params.get("summary") == "1",
                    event_limit=limit,
                ),
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/api/observability/generation")
    async def generation(request: Request):
        database, auth = services()
        user = await auth.require_session(request)
        if user["role"] != "admin":
            raise AuthorizationError("ADMIN_REQUIRED", 403, "需要管理员权限。")
        if user["must_change_password"]:
            raise AuthorizationError("PASSWORD_CHANGE_REQUIRED", 428, "请先修改初始密码。")
        return JSONResponse(
            {"success": True, "data": await generation_dashboard(Projects(database, root))},
            headers={"Cache-Control": "private, no-store"},
        )

    return router
