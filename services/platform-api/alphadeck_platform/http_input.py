import json

from .api_errors import ApiError


async def bounded_body(request, maximum):
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > maximum:
            raise ApiError(413, "REQUEST_TOO_LARGE", "Request body is too large.")
        content.extend(chunk)
    return bytes(content)


def reject_constant(value):
    raise ValueError("Non-finite JSON number: " + value)


async def bounded_json(request, maximum):
    content = await bounded_body(request, maximum)
    try:
        return json.loads(content, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as cause:
        raise ApiError(400, "INVALID_JSON", "Request body must be valid JSON.") from cause
