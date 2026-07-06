import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from .auth import AuthorizationError, active_ban, verify_cookie
from .config import ROOT
from .llm_config import bounded_integer, env_flag
from .quota import advisory, one, utc
from .sessions import iso


class AuthApiError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class AuthOptions:
    def __init__(self):
        root = json.loads((ROOT / "config/auth.json").read_text())
        if root.get("schemaVersion") != 1:
            raise ValueError("Unsupported authentication schema")
        self.local = deepcopy(root["local"])
        self.session = self.local["session"]
        for key, environment, minimum, maximum in (
            ("expiresInSeconds", "ALPHADECK_AUTH_SESSION_EXPIRES_SECONDS", 900, 2592000),
            ("updateAgeSeconds", "ALPHADECK_AUTH_SESSION_UPDATE_AGE_SECONDS", 60, 86400),
            ("freshAgeSeconds", "ALPHADECK_AUTH_SESSION_FRESH_AGE_SECONDS", 60, 86400),
        ):
            self.session[key] = bounded_integer(environment, self.session[key], minimum, maximum)
        self.session["rememberMe"] = env_flag(
            "ALPHADECK_AUTH_REMEMBER_ME", self.session["rememberMe"]
        )
        self.secure = os.getenv("NODE_ENV") == "production" or env_flag(
            "ALPHADECK_AUTH_SECURE_COOKIES", False
        )
        self.allow_signup = env_flag("ALPHADECK_AUTH_ALLOW_SIGNUP", self.local["allowSignUp"])
        self.base_url = (
            os.getenv("BETTER_AUTH_URL")
            or os.getenv("NEXT_PUBLIC_APP_URL")
            or "http://localhost:3100"
        ).rstrip("/")
        self.trusted_origins = {self.origin(self.base_url)}
        for value in os.getenv("ALPHADECK_AUTH_TRUSTED_ORIGINS", "").split(","):
            value = value.strip()
            if value:
                parsed = urlsplit(value)
                if parsed.scheme == "https" or parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
                    self.trusted_origins.add(self.origin(value))
        password = self.local["password"]
        parameters = password["argon2id"]
        if (
            not 12 <= password["minLength"] <= 64
            or not password["minLength"] <= password["maxLength"] <= 256
            or not 19456 <= parameters["memoryCostKiB"] <= 1048576
            or not 2 <= parameters["timeCost"] <= 10
            or not 1 <= parameters["parallelism"] <= 16
            or not 32 <= parameters["outputLength"] <= 128
        ):
            raise ValueError("Invalid password policy")
        self.hasher = PasswordHasher(
            time_cost=parameters["timeCost"],
            memory_cost=parameters["memoryCostKiB"],
            parallelism=parameters["parallelism"],
            hash_len=parameters["outputLength"],
            type=Type.ID,
        )

    @staticmethod
    def origin(value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Invalid authentication origin")
        port = parsed.port
        authority = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
            authority += ":" + str(port)
        return parsed.scheme + "://" + authority

    async def hash_password(self, password, *, development_default=False):
        if not isinstance(password, str):
            raise AuthApiError("INVALID_PASSWORD", "Invalid password")
        if (
            not self.local["password"]["minLength"]
            <= len(password)
            <= self.local["password"]["maxLength"]
        ):
            local = urlsplit(self.base_url).hostname in {"127.0.0.1", "localhost", "::1"}
            if not (
                development_default
                and password == "admin"
                and local
                and os.getenv("NODE_ENV") != "production"
                and os.getenv("ALPHADECK_DEGRADATION_MODE") != "strict"
            ):
                raise AuthApiError("INVALID_PASSWORD", "密码长度不符合当前策略。")
        return await asyncio.to_thread(self.hasher.hash, password)

    async def verify(self, hashed, password):
        if not hashed or not isinstance(password, str) or not password:
            return False
        try:
            return await asyncio.to_thread(self.hasher.verify, hashed, password)
        except (VerificationError, InvalidHashError):
            return False


def public_user(user):
    value = {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "emailVerified": user["email_verified"],
        "image": user["image"],
        "role": user["role"],
        "banned": user["banned"],
        "banReason": user["ban_reason"],
        "mustChangePassword": user["must_change_password"],
    }
    for name, field in (
        ("createdAt", "created_at"),
        ("updatedAt", "updated_at"),
        ("banExpires", "ban_expires"),
        ("lastLoginAt", "last_login_at"),
        ("passwordChangedAt", "password_changed_at"),
    ):
        value[name] = iso(user[field]) if user[field] else None
    return value


def public_session(user):
    return {
        "id": user["session_id"],
        "token": user["session_token"],
        "userId": user["id"],
        "expiresAt": iso(user["session_expires_at"]),
        "createdAt": iso(user["session_created_at"]),
        "updatedAt": iso(user["session_updated_at"]),
        "ipAddress": user["ip_address"],
        "userAgent": user["user_agent"],
        "impersonatedBy": user["session_impersonated_by"],
    }


def client_ip(request):
    address = request.client.host if request.client else None
    # The Python listener accepts local gateway traffic. Trust only the final forwarded hop of that gateway.
    if address in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
    return address or "no-trusted-ip"


async def audit(
    database,
    request,
    event,
    *,
    actor=None,
    target_type=None,
    target_id=None,
    outcome="success",
    metadata=None,
):
    try:
        async with database.transaction() as connection:
            await connection.execute(
                """INSERT INTO auth_audit_events
                (id,actor_user_id,event_type,target_type,target_id,outcome,ip_address,user_agent,metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    str(uuid4()),
                    actor,
                    event,
                    target_type,
                    target_id,
                    outcome,
                    client_ip(request),
                    request.headers.get("user-agent", "")[:512] or None,
                    Jsonb(metadata or {}),
                ),
            )
    except Exception:
        logging.exception("Authentication audit persistence failed")


class Credentials:
    def __init__(self, database, authorization, options=None):
        self.database, self.authorization = database, authorization
        self.options = options or AuthOptions()

    def check_origin(self, request):
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" and not origin:
            raise AuthApiError("INVALID_ORIGIN", "Invalid origin", 403)
        if origin:
            try:
                allowed = self.options.origin(origin) in self.options.trusted_origins
            except ValueError:
                allowed = False
            if not allowed:
                raise AuthApiError("INVALID_ORIGIN", "Invalid origin", 403)

    def cookie_name(self, suffix):
        return ("__Secure-" if self.options.secure else "") + "alphadeck." + suffix

    def sign(self, value):
        signature = base64.b64encode(
            hmac.digest(self.authorization.secret.encode(), value.encode(), "sha256")
        ).decode()
        return quote(value + "." + signature, safe="")

    def set_session_cookie(self, response, token, remember):
        response.set_cookie(
            self.cookie_name("session_token"),
            self.sign(token),
            max_age=self.options.session["expiresInSeconds"] if remember else None,
            httponly=True,
            secure=self.options.secure,
            samesite="lax",
            path="/",
        )
        if not remember:
            response.set_cookie(
                self.cookie_name("dont_remember"),
                self.sign("true"),
                httponly=True,
                secure=self.options.secure,
                samesite="lax",
                path="/",
            )
        else:
            response.delete_cookie(
                self.cookie_name("dont_remember"),
                path="/",
                secure=self.options.secure,
                httponly=True,
                samesite="lax",
            )

    async def rate_limit(self, request, path):
        rules = self.options.local["rateLimit"]
        maximum, seconds = rules["maxRequests"], rules["windowSeconds"]
        if path.startswith(("/sign-in", "/sign-up", "/change-password", "/change-email")):
            maximum, seconds = 3, 10
        if path == "/sign-in/email":
            maximum, seconds = rules["signInMaxRequests"], rules["signInWindowSeconds"]
        key = client_ip(request) + "|" + path
        milliseconds = int(datetime.now(UTC).timestamp() * 1000)
        async with self.database.transaction() as connection:
            await advisory(connection, "auth-rate-limit", key)
            row = await one(
                connection, "SELECT * FROM auth_rate_limits WHERE key=%s FOR UPDATE", (key,)
            )
            if (
                row
                and milliseconds - row["last_request"] <= seconds * 1000
                and row["count"] >= maximum
            ):
                retry = max(
                    1, math.ceil((row["last_request"] + seconds * 1000 - milliseconds) / 1000)
                )
                return JSONResponse(
                    {"message": "Too many requests. Please try again later."},
                    status_code=429,
                    headers={"X-Retry-After": str(retry)},
                )
            count = (
                row["count"] + 1
                if row and milliseconds - row["last_request"] <= seconds * 1000
                else 1
            )
            await connection.execute(
                "INSERT INTO auth_rate_limits (id,key,count,last_request) VALUES (%s,%s,%s,%s) ON CONFLICT (key) DO UPDATE SET count=excluded.count,last_request=excluded.last_request",
                (str(uuid4()), key, count, milliseconds),
            )
        return None

    async def create_session(self, connection, request, user_id, remember=True):
        timestamp = datetime.now(UTC)
        lifetime = self.options.session["expiresInSeconds"] if remember else 86400
        return await one(
            connection,
            """INSERT INTO auth_sessions
            (id,user_id,token,expires_at,created_at,updated_at,ip_address,user_agent)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                str(uuid4()),
                user_id,
                secrets.token_urlsafe(24),
                timestamp + timedelta(seconds=lifetime),
                timestamp,
                timestamp,
                client_ip(request),
                request.headers.get("user-agent", ""),
            ),
        )

    async def sign_in(self, request, body):
        email, password = body.get("email"), body.get("password")
        if (
            not isinstance(email, str)
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
            or len(email) > 320
        ):
            raise AuthApiError("INVALID_EMAIL", "Invalid email")
        if not isinstance(password, str) or not password or len(password) > 256:
            raise AuthApiError("INVALID_EMAIL_OR_PASSWORD", "Invalid email or password", 401)
        if "rememberMe" in body and type(body["rememberMe"]) is not bool:
            raise AuthApiError("VALIDATION_ERROR", "Invalid rememberMe value")
        email = email.strip().lower()
        user = await self.database.fetch_one(
            "SELECT u.*,a.password AS credential_password FROM auth_users u LEFT JOIN auth_accounts a ON a.user_id=u.id AND a.provider_id='credential' WHERE u.email=%s ORDER BY a.id LIMIT 1",
            (email,),
        )
        if not user or not user["credential_password"]:
            await asyncio.to_thread(self.options.hasher.hash, password)
            raise AuthApiError("INVALID_EMAIL_OR_PASSWORD", "Invalid email or password", 401)
        if not await self.options.verify(user["credential_password"], password):
            raise AuthApiError("INVALID_EMAIL_OR_PASSWORD", "Invalid email or password", 401)
        callback = body.get("callbackURL")
        if callback:
            if not isinstance(callback, str) or callback.startswith("//"):
                raise AuthApiError("INVALID_CALLBACK_URL", "Invalid callback URL", 403)
            if not callback.startswith("/"):
                try:
                    trusted = self.options.origin(callback) in self.options.trusted_origins
                except ValueError:
                    trusted = False
                if not trusted:
                    raise AuthApiError("INVALID_CALLBACK_URL", "Invalid callback URL", 403)
        remember = body.get("rememberMe", True)
        async with self.database.transaction() as connection:
            current = await one(
                connection, "SELECT * FROM auth_users WHERE id=%s FOR UPDATE", (user["id"],)
            )
            credential = await one(
                connection,
                "SELECT password FROM auth_accounts WHERE user_id=%s AND provider_id='credential' ORDER BY id LIMIT 1",
                (user["id"],),
            )
            if (
                not current
                or not credential
                or credential["password"] != user["credential_password"]
            ):
                raise AuthApiError("INVALID_EMAIL_OR_PASSWORD", "Invalid email or password", 401)
            if active_ban(current, datetime.now(UTC)):
                raise AuthApiError("BANNED_USER", "账号已被停用，请联系 AlphaDeck 管理员。", 403)
            if current["banned"]:
                current = await one(
                    connection,
                    "UPDATE auth_users SET banned=false,ban_reason=NULL,ban_expires=NULL,updated_at=now() WHERE id=%s RETURNING *",
                    (user["id"],),
                )
            session = await self.create_session(connection, request, user["id"], remember)
            await connection.execute(
                "UPDATE auth_users SET last_login_at=now(),updated_at=now() WHERE id=%s",
                (user["id"],),
            )
        payload = {
            "redirect": bool(callback),
            "token": session["token"],
            "user": public_user(current),
        }
        if callback is not None:
            payload["url"] = callback
        response = JSONResponse(payload)
        if callback:
            response.headers["Location"] = callback
        self.set_session_cookie(response, session["token"], remember)
        return response, user["id"]

    async def session_response(self, request):
        user = await self.authorization.session(request)
        if not user:
            return JSONResponse(None)
        dont_remember = request.cookies.get(self.cookie_name("dont_remember"))
        skip = request.query_params.get("disableRefresh") in {"true", "1"} or (
            dont_remember and verify_cookie(dont_remember, self.authorization.secret) == "true"
        )
        due = utc(user["session_expires_at"]) - timedelta(
            seconds=self.options.session["expiresInSeconds"]
            - self.options.session["updateAgeSeconds"]
        ) <= datetime.now(UTC)
        response = None
        if not skip and due:
            async with self.database.transaction() as connection:
                row = await one(
                    connection,
                    "UPDATE auth_sessions SET expires_at=%s,updated_at=now() WHERE id=%s AND expires_at>now() RETURNING *",
                    (
                        datetime.now(UTC)
                        + timedelta(seconds=self.options.session["expiresInSeconds"]),
                        user["session_id"],
                    ),
                )
                if not row:
                    raise AuthApiError("FAILED_TO_GET_SESSION", "Failed to get session", 401)
            user["session_expires_at"], user["session_updated_at"] = (
                row["expires_at"],
                row["updated_at"],
            )
            response = JSONResponse({"session": public_session(user), "user": public_user(user)})
            self.set_session_cookie(response, user["session_token"], True)
        return response or JSONResponse(
            {"session": public_session(user), "user": public_user(user)}
        )

    async def sign_out(self, request):
        user = await self.authorization.session(request)
        if user:
            async with self.database.transaction() as connection:
                await connection.execute(
                    "DELETE FROM auth_sessions WHERE id=%s AND user_id=%s",
                    (user["session_id"], user["id"]),
                )
        response = JSONResponse({"success": True})
        for name in ("session_token", "session_data", "dont_remember", "account_data"):
            response.delete_cookie(
                self.cookie_name(name),
                path="/",
                secure=self.options.secure,
                httponly=True,
                samesite="lax",
            )
        await audit(
            self.database,
            request,
            "authentication.logout",
            actor=user["id"] if user else None,
            target_type="session",
            target_id=user["session_id"] if user else None,
        )
        return response

    async def change_password(
        self,
        request,
        user,
        current_password,
        new_password,
        *,
        revoke_others=True,
        rotate_session=False,
    ):
        account = await self.database.fetch_one(
            "SELECT * FROM auth_accounts WHERE user_id=%s AND provider_id='credential' ORDER BY id LIMIT 1",
            (user["id"],),
        )
        if not account or not await self.options.verify(account["password"], current_password):
            raise AuthApiError("INVALID_PASSWORD", "Invalid password")
        hashed = await self.options.hash_password(new_password)
        async with self.database.transaction() as connection:
            current = await one(
                connection, "SELECT * FROM auth_users WHERE id=%s FOR UPDATE", (user["id"],)
            )
            locked = await one(
                connection,
                "SELECT password FROM auth_accounts WHERE id=%s FOR UPDATE",
                (account["id"],),
            )
            session = await one(
                connection,
                "SELECT id FROM auth_sessions WHERE id=%s AND user_id=%s AND expires_at>now()",
                (user["session_id"], user["id"]),
            )
            if not locked or locked["password"] != account["password"] or not session:
                raise AuthApiError(
                    "INVALID_PASSWORD", "Password or session changed during this operation"
                )
            if active_ban(current, datetime.now(UTC)):
                raise AuthorizationError("ACCOUNT_DISABLED", 403, "账号已被停用。")
            await connection.execute(
                "UPDATE auth_accounts SET password=%s,updated_at=now() WHERE id=%s",
                (hashed, account["id"]),
            )
            await connection.execute(
                "UPDATE auth_users SET must_change_password=false,password_changed_at=now(),updated_at=now() WHERE id=%s",
                (user["id"],),
            )
            revoked = 0
            if revoke_others:
                result = await connection.execute(
                    "DELETE FROM auth_sessions WHERE user_id=%s AND id<>%s",
                    (user["id"], user["session_id"]),
                )
                revoked = result.rowcount
            rotated = None
            if rotate_session:
                await connection.execute(
                    "DELETE FROM auth_sessions WHERE user_id=%s", (user["id"],)
                )
                rotated = await self.create_session(connection, request, user["id"])
            changed = await one(connection, "SELECT * FROM auth_users WHERE id=%s", (user["id"],))
        await audit(
            self.database,
            request,
            "user.password_changed",
            actor=user["id"],
            target_type="user",
            target_id=user["id"],
            metadata={"revokedOtherSessions": revoked},
        )
        return changed, rotated
