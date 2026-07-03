from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.formparsers import MultiPartException, MultiPartParser

from .http_input import bounded_body, bounded_json
from .image_assets import (
    MIME,
    ImageAssetError,
    decode_base64,
    maximum_bytes,
    read_asset,
    save_asset,
)
from .projects import Projects


def asset_routes(services, root):
    router = APIRouter()

    async def workspace(request, project, action):
        database, auth = services()
        await auth.require_action(request, action, project)
        projects = Projects(database, root)
        return projects.managed_path(await projects.get(project), must_exist=True)

    @router.post("/api/assets/{project_id}/upload")
    async def upload(request: Request, project_id: str):
        path = await workspace(request, project_id, "project.source.write")
        maximum = maximum_bytes()
        content = await bounded_body(request, maximum + 65536)

        async def chunks():
            yield content

        try:
            form = await MultiPartParser(
                request.headers, chunks(), max_files=1, max_fields=5, max_part_size=maximum
            ).parse()
        except MultiPartException:
            return JSONResponse(
                {"success": False, "error": "Invalid image upload form"}, status_code=400
            )
        try:
            file = form.get("file")
            if not callable(getattr(file, "read", None)):
                return JSONResponse(
                    {"success": False, "error": "File field is required"}, status_code=400
                )
            if file.content_type not in MIME:
                raise ImageAssetError("File must be a PNG, JPEG, WebP, or GIF image")
            filename = await save_asset(path, await file.read(maximum + 1), file.content_type)
            return {
                "success": True,
                "path": "assets/" + filename,
                "filename": filename,
                "originalFilename": file.filename or "image.png",
                "publicPath": "public/uploads/" + filename,
                "publicUrl": "/uploads/" + filename,
            }
        finally:
            await form.close()

    @router.post("/api/assets/{project_id}/logo")
    async def logo(request: Request, project_id: str):
        path = await workspace(request, project_id, "project.source.write")
        body = await bounded_json(request, (maximum_bytes() + 2) // 3 * 4 + 65536)
        value = body.get("b64_png") if isinstance(body, dict) else None
        if not isinstance(value, str) or not value:
            return JSONResponse({"success": False, "error": "b64_png is required"}, status_code=400)
        await save_asset(path, decode_base64(value, required="image/png"), "image/png", logo=True)
        return {"success": True, "path": "assets/logo.png"}

    @router.get("/api/assets/{project_id}/{filename}")
    async def get(request: Request, project_id: str, filename: str):
        path = await workspace(request, project_id, "project.source.read")
        content, detected = read_asset(path, filename)
        return Response(
            content,
            media_type=detected,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    return router
