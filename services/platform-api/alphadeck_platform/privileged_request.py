import hmac
import os
from urllib.parse import urlsplit

from .api_errors import ApiError

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def origin(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
        port = parsed.port
        if port == (80 if parsed.scheme == "http" else 443):
            port = None
        hostname = parsed.hostname.lower()
        host = "[" + hostname + "]" if ":" in hostname else hostname
        return parsed.scheme.lower() + "://" + host + (":" + str(port) if port else "")
    except ValueError as cause:
        raise ApiError(403, "INVALID_ORIGIN", "请求 Origin 无效。") from cause


def hostname(value):
    try:
        return (urlsplit(value if "://" in value else "http://" + value).hostname or "").lower()
    except ValueError:
        return ""


def same_origin_mutation(request):
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise ApiError(403, "CROSS_SITE_REQUEST", "拒绝跨站管理请求。")
    if provided := request.headers.get("origin"):
        host = (
            (
                request.headers.get("x-forwarded-host")
                or request.headers.get("host")
                or request.url.netloc
            )
            .split(",")[0]
            .strip()
        )
        protocol = (
            (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
        )
        try:
            expected = origin(protocol + "://" + host)
        except ApiError:
            expected = origin(str(request.url))
        if origin(provided) != expected:
            raise ApiError(403, "ORIGIN_MISMATCH", "管理请求必须来自同源页面。")


def privileged_mutation(request, env=None):
    env = os.environ if env is None else env
    same_origin_mutation(request)
    expected = env.get("ALPHADECK_ADMIN_TOKEN", "").strip()
    authorization = request.headers.get("authorization", "").strip()
    provided = request.headers.get("x-alphadeck-admin-token", "").strip() or (
        authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    )
    if expected:
        if not provided or not hmac.compare_digest(expected.encode(), provided.encode()):
            raise ApiError(401, "INVALID_ADMIN_TOKEN", "缺少有效的 AlphaDeck 管理令牌。")
        return
    strict = (
        env.get("NODE_ENV") == "production" or env.get("ALPHADECK_DEGRADATION_MODE") == "strict"
    )
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if strict or request.url.hostname not in LOOPBACK or hostname(host) not in LOOPBACK:
        raise ApiError(
            403, "ADMIN_TOKEN_REQUIRED", "管理接口未配置 ALPHADECK_ADMIN_TOKEN，已拒绝非本机写入。"
        )


def authorized_skill_mutation(request, context, env=None):
    if context.user and not context.local_system_admin:
        if not request.headers.get("origin"):
            raise ApiError(403, "ORIGIN_REQUIRED", "浏览器写入请求缺少 Origin。")
        same_origin_mutation(request)
    else:
        privileged_mutation(request, env)
