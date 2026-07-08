import logging
import re
from contextlib import suppress
from uuid import uuid4

from .api_errors import ApiError
from .messages import js_length, js_slice
from .quota import one
from .resource_lock import resource_lock, sync_directory
from .scaffold import controlled_path
from .secret_crypto import decrypt, encrypt
from .skill_integrity import read_regular
from .workspace import write_atomic


def parse_write(value, *, update=False):
    fields = (
        {"value"} if update else {"key", "value", "scope", "varType", "isSecret", "description"}
    )
    required = {"value"} if update else {"key", "value"}
    if not isinstance(value, dict) or set(value) - fields or not required <= value.keys():
        raise ApiError(400, "INVALID_ENV_REQUEST", "Invalid environment variable request.")
    result = dict(value)
    if not isinstance(value["value"], str) or js_length(value["value"]) > 1000000:
        raise ApiError(400, "INVALID_ENV_REQUEST", "Invalid environment variable value.")
    if update:
        return result
    key = value["key"].strip() if isinstance(value["key"], str) else ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key):
        raise ApiError(400, "INVALID_ENV_REQUEST", "Invalid environment variable key.")
    result.update(
        key=key,
        scope=value.get("scope", "runtime"),
        varType=value.get("varType", "string"),
        isSecret=value.get("isSecret", True),
    )
    if (
        result["scope"] not in ("runtime", "build", "both")
        or result["varType"] not in ("string", "number", "boolean", "json")
        or type(result["isSecret"]) is not bool
    ):
        raise ApiError(400, "INVALID_ENV_REQUEST", "Invalid environment variable options.")
    if "description" in result and result["description"] is not None:
        if (
            not isinstance(result["description"], str)
            or js_length(result["description"].strip()) > 2000
        ):
            raise ApiError(400, "INVALID_ENV_REQUEST", "Invalid environment variable description.")
        result["description"] = result["description"].strip()
    return result


def record(row, *, reveal=False):
    value = decrypt(row["value_encrypted"])
    return {
        "id": row["id"],
        "key": row["key"],
        "value": value if reveal or not row["is_secret"] else "",
        "valuePreview": value
        if not row["is_secret"]
        else ""
        if not value
        else "••••"
        if js_length(value) <= 8
        else js_slice(value, 0, 2) + "••••" + js_slice(value, -2),
        "hasValue": bool(value),
        "scope": row["scope"],
        "varType": row["var_type"],
        "isSecret": row["is_secret"],
        "description": row["description"],
    }


def parse_file(contents):
    result = {}
    for line in re.split(r"\r?\n", contents):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if match := re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line):
            value = match[2]
            result[match[1]] = (
                value[1:-1]
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"
                else value
            )
    return result


def render_file(entries):
    lines = []
    for item in entries:
        value = item["value"]
        quoted = '"' + value.replace('"', '\\"') + '"' if re.search(r"[ \t#\"$']", value) else value
        lines.append(item["key"] + "=" + quoted)
    return (
        "# Environment Variables\n# This file is automatically synchronized with Project Settings\n\n"
        + "\n".join(lines)
        + ("\n" if entries else "")
    )


def read_file(root):
    try:
        return parse_file(
            read_regular(controlled_path(root, ".env"), 8388608).decode("utf-8", "replace")
        )
    except FileNotFoundError:
        return None


async def mapped(tx, project, *, reveal=False):
    rows = await (
        await tx.execute("SELECT * FROM env_vars WHERE project_id=%s ORDER BY key", (project,))
    ).fetchall()
    result = []
    for row in rows:
        try:
            result.append(record(row, reveal=reveal))
        except ValueError:
            logging.warning("Unable to decrypt a project environment variable")
    return result


class Environment:
    def __init__(self, projects):
        self.projects, self.database = projects, projects.database

    async def list(self, project, *, reveal=False):
        async with self.database.transaction() as tx:
            return await mapped(tx, project, reveal=reveal)

    async def mutate(self, project, operation):
        root = self.projects.managed_path(await self.projects.get(project), must_exist=True)
        async with (
            resource_lock(root, {"purpose": "environment_sync"}, recover_dead_local_owner=True),
            self.database.transaction() as tx,
        ):
            if not await one(tx, "SELECT id FROM projects WHERE id=%s FOR UPDATE", (project,)):
                raise ApiError(404, "PROJECT_NOT_FOUND", "Project not found")
            return await operation(tx, root)

    async def sync_to_file(self, tx, root, project):
        entries = await mapped(tx, project, reveal=True)
        controlled_path(root, ".env")
        target = write_atomic(root, ".env", render_file(entries).encode())
        target.chmod(0o600)
        sync_directory(target.parent)
        return len(entries)

    async def write(self, project, value, *, upsert=False):
        async def operation(tx, root):
            if not upsert and await one(
                tx,
                "SELECT id FROM env_vars WHERE project_id=%s AND key=%s",
                (project, value["key"]),
            ):
                raise ApiError(
                    409,
                    "ENV_ALREADY_EXISTS",
                    f'Environment variable "{value["key"]}" already exists',
                )
            row = await one(
                tx,
                """INSERT INTO env_vars (id,project_id,key,value_encrypted,scope,var_type,is_secret,description,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())
                ON CONFLICT (project_id,key) DO UPDATE SET value_encrypted=EXCLUDED.value_encrypted,scope=EXCLUDED.scope,
                var_type=EXCLUDED.var_type,is_secret=EXCLUDED.is_secret,
                description=CASE WHEN %s THEN EXCLUDED.description ELSE env_vars.description END,updated_at=clock_timestamp() RETURNING *""",
                (
                    str(uuid4()),
                    project,
                    value["key"],
                    encrypt(value["value"]),
                    value["scope"],
                    value["varType"],
                    value["isSecret"],
                    value.get("description"),
                    "description" in value,
                ),
            )
            await self.sync_to_file(tx, root, project)
            return record(row)

        return await self.mutate(project, operation)

    async def update(self, project, key, value=None, *, delete=False):
        async def operation(tx, root):
            result = await tx.execute(
                "DELETE FROM env_vars WHERE project_id=%s AND key=%s"
                if delete
                else "UPDATE env_vars SET value_encrypted=%s,updated_at=clock_timestamp() WHERE project_id=%s AND key=%s",
                (project, key) if delete else (encrypt(value), project, key),
            )
            if result.rowcount:
                await self.sync_to_file(tx, root, project)
            return result.rowcount > 0

        return await self.mutate(project, operation)

    async def synchronize(self, project, *, from_file=False):
        async def operation(tx, root):
            if not from_file:
                return await self.sync_to_file(tx, root, project)
            values = read_file(root)
            if values is None:
                return 0
            rows = await (
                await tx.execute("SELECT * FROM env_vars WHERE project_id=%s", (project,))
            ).fetchall()
            existing, changes = {row["key"]: row for row in rows}, 0
            for key, value in values.items():
                old = None
                if key in existing:
                    with suppress(ValueError):
                        old = decrypt(existing[key]["value_encrypted"])
                if old == value:
                    continue
                await tx.execute(
                    "INSERT INTO env_vars (id,project_id,key,value_encrypted,updated_at) VALUES (%s,%s,%s,%s,clock_timestamp()) ON CONFLICT (project_id,key) DO UPDATE SET value_encrypted=EXCLUDED.value_encrypted,updated_at=clock_timestamp()",
                    (str(uuid4()), project, key, encrypt(value)),
                )
                changes += 1
            for key in existing.keys() - values.keys():
                await tx.execute(
                    "DELETE FROM env_vars WHERE project_id=%s AND key=%s", (project, key)
                )
                changes += 1
            return changes

        return await self.mutate(project, operation)

    async def conflicts(self, project):
        root = self.projects.managed_path(await self.projects.get(project), must_exist=True)
        values = read_file(root) or {}
        db_values = {item["key"]: item["value"] for item in await self.list(project, reveal=True)}
        conflicts = []
        for key in dict.fromkeys([*values, *db_values]):
            if key in values and key in db_values and values[key] == db_values[key]:
                continue
            conflicts.append(
                {
                    "key": key,
                    **({"file_value": values[key]} if key in values else {}),
                    **({"db_value": db_values[key]} if key in db_values else {}),
                    "conflict_type": "file_only"
                    if key not in db_values
                    else "db_only"
                    if key not in values
                    else "value_mismatch",
                }
            )
        return {"conflicts": conflicts, "has_conflicts": bool(conflicts)}
