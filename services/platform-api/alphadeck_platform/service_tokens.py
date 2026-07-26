from uuid import uuid4

from .api_errors import ApiError
from .messages import js_length, js_slice
from .quota import advisory, one
from .secret_crypto import decrypt, encrypt
from .sessions import iso

PROVIDERS = {"github", "supabase", "vercel"}


def provider(value):
    if not isinstance(value, str) or value not in PROVIDERS:
        raise ApiError(400, "INVALID_PROVIDER", "Invalid provider")
    return value


def plain(value):
    return decrypt(value) if value.startswith("v2:") else value


def response(row, token):
    return {
        "id": row["id"],
        "provider": row["provider"],
        "name": row["name"],
        "token": None,
        "token_preview": "••••"
        if js_length(token) <= 8
        else js_slice(token, 0, 4) + "••••" + js_slice(token, -4),
        "has_token": bool(token),
        "created_at": iso(row["created_at"]),
        "last_used": iso(row["last_used"]) if row["last_used"] else None,
    }


class ServiceTokens:
    def __init__(self, database):
        self.database = database

    async def create(self, identity, token, name=""):
        provider(identity)
        if (
            not isinstance(token, str)
            or not token.strip()
            or js_length(token) > 65536
            or not isinstance(name, str)
            or js_length(name) > 120
        ):
            raise ApiError(400, "INVALID_TOKEN_REQUEST", "Invalid service token request.")
        token = token.strip()
        async with self.database.transaction() as tx:
            await advisory(tx, "service-token", identity)
            await tx.execute("DELETE FROM service_tokens WHERE provider=%s", (identity,))
            row = await one(
                tx,
                "INSERT INTO service_tokens (id,provider,name,token,updated_at) VALUES (%s,%s,%s,%s,clock_timestamp()) RETURNING *",
                (
                    str(uuid4()),
                    identity,
                    name.strip() or identity.capitalize() + " Token",
                    encrypt(token),
                ),
            )
            return response(row, token)

    async def get(self, identity, *, reveal=False, touch=False):
        provider(identity)
        async with self.database.transaction() as tx:
            await advisory(tx, "service-token", identity)
            row = await one(
                tx,
                "SELECT * FROM service_tokens WHERE provider=%s ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
                (identity,),
            )
            if not row:
                return None
            token = plain(row["token"])
            if not row["token"].startswith("v2:"):
                await tx.execute(
                    "UPDATE service_tokens SET token=%s,updated_at=clock_timestamp() WHERE id=%s",
                    (encrypt(token), row["id"]),
                )
            if touch:
                await tx.execute(
                    "UPDATE service_tokens SET last_used=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s",
                    (row["id"],),
                )
            return token if reveal else response(row, token)

    async def delete(self, identity):
        async with self.database.transaction() as tx:
            return (
                await tx.execute("DELETE FROM service_tokens WHERE id=%s", (identity,))
            ).rowcount == 1
