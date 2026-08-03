import hmac
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .api_errors import ApiError
from .http_input import bounded_json
from .service_tokens import ServiceTokens


def token_routes(services):
    router = APIRouter()

    def result(value, status=200):
        return JSONResponse(
            value, status_code=status, headers={"Cache-Control": "private, no-store"}
        )

    @router.post("/api/tokens")
    async def create(request: Request):
        database, auth = services()
        await auth.require_action(request, "platform.tokens.manage")
        value = await bounded_json(request, 400000)
        if (
            not isinstance(value, dict)
            or set(value) - {"provider", "token", "name"}
            or not {"provider", "token"} <= value.keys()
        ):
            raise ApiError(400, "INVALID_TOKEN_REQUEST", "Invalid service token request.")
        record = await ServiceTokens(database).create(
            value["provider"], value["token"], value.get("name", "")
        )
        return result({"success": True, "data": record}, 201)

    @router.get("/api/tokens/{segments:path}")
    async def get(request: Request, segments: str):
        parts = segments.split("/")
        database, auth = services()
        if len(parts) == 1:
            await auth.require_action(request, "platform.tokens.manage")
            record = await ServiceTokens(database).get(parts[0])
            return (
                result(record)
                if record
                else result({"success": False, "error": "Token not found"}, 404)
            )
        if len(parts) == 3 and parts[0] == "internal" and parts[2] == "token":
            if os.getenv("ALPHADECK_ENABLE_INTERNAL_TOKEN_API") != "1":
                return result({"success": False, "error": "Not found"}, 404)
            expected, provided = (
                os.getenv("ALPHADECK_INTERNAL_API_TOKEN", ""),
                request.headers.get("x-alphadeck-internal-token", ""),
            )
            if (
                not expected
                or not provided
                or not hmac.compare_digest(expected.encode(), provided.encode())
            ):
                return result({"success": False, "error": "Forbidden"}, 403)
            token = await ServiceTokens(database).get(parts[1], reveal=True, touch=True)
            return (
                result({"token": token})
                if token
                else result({"success": False, "error": "Token not found"}, 404)
            )
        return result({"success": False, "error": "Not found"}, 404)

    @router.delete("/api/tokens/{segments:path}")
    async def delete(request: Request, segments: str):
        if "/" in segments:
            return result({"success": False, "error": "Not found"}, 404)
        database, auth = services()
        await auth.require_action(request, "platform.tokens.manage")
        deleted = await ServiceTokens(database).delete(segments)
        return (
            result({"success": True, "message": "Token deleted successfully"})
            if deleted
            else result({"success": False, "error": "Token not found"}, 404)
        )

    return router
