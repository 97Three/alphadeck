from datetime import timedelta

from psycopg.errors import ForeignKeyViolation

from .quota import one, utc
from .sessions import iso

STAGES = {"planning_data_prefetch", "agent_execution", "manual_validation"}


class GenerationLeaseError(Exception):
    def __init__(self, code, message, request_id=None, stage=None):
        self.code, self.message, self.request_id, self.stage = code, message, request_id, stage
        super().__init__(message)


def identifier(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode()) > 256
        or "\n" in value
        or "\r" in value
    ):
        raise GenerationLeaseError(
            "GENERATION_LEASE_INVALID", "Lease identifiers must be bounded single-line strings."
        )


def ttl(value):
    if type(value) is not int or not 0 < value <= 86400000:
        raise GenerationLeaseError(
            "GENERATION_LEASE_INVALID", "Lease TTL must be between 1 and 86400000 milliseconds."
        )


async def database_now(connection):
    return utc((await one(connection, "SELECT clock_timestamp() AS value"))["value"])


async def lock_lease(connection, project):
    row = await one(
        connection,
        "SELECT * FROM agent_generation_leases WHERE project_id=%s FOR UPDATE",
        (project,),
    )
    if not row:
        raise GenerationLeaseError(
            "GENERATION_PROJECT_NOT_FOUND", "Generation project does not exist."
        )
    return row


def check_fence(row, fence, now):
    if (
        row["status"] != "held"
        or row["project_id"] != fence["projectId"]
        or row["operation_id"] != fence["operationId"]
        or row["lease_owner"] != fence["leaseOwner"]
        or row["fencing_token"] != fence["fencingToken"]
    ):
        raise GenerationLeaseError(
            "GENERATION_LEASE_LOST",
            "The orchestrator no longer owns the generation fence.",
            row["active_request_id"],
            row["stage"],
        )
    if not row["lease_expires_at"] or utc(row["lease_expires_at"]) <= now:
        raise GenerationLeaseError(
            "GENERATION_LEASE_EXPIRED",
            "The project generation lease expired before its durable write.",
            row["active_request_id"],
            row["stage"],
        )


class GenerationLease:
    def __init__(self, database):
        self.database = database

    async def claim(
        self, project, operation, owner, lease_ttl_ms, *, request=None, stage="agent_execution"
    ):
        for value in (project, operation, owner):
            identifier(value)
        if request:
            identifier(request)
        ttl(lease_ttl_ms)
        if stage not in STAGES:
            raise GenerationLeaseError("GENERATION_LEASE_INVALID", "Unsupported generation stage.")
        try:
            async with self.database.transaction() as connection:
                if request and not await one(
                    connection,
                    "SELECT id FROM user_requests WHERE id=%s AND project_id=%s",
                    (request, project),
                ):
                    raise GenerationLeaseError(
                        "GENERATION_REQUEST_NOT_FOUND",
                        "Generation request does not exist or belongs to another project.",
                    )
                await connection.execute(
                    "INSERT INTO agent_generation_leases (project_id,updated_at) VALUES (%s,now()) ON CONFLICT DO NOTHING",
                    (project,),
                )
                current = await lock_lease(connection, project)
                now = await database_now(connection)
                if (
                    current["status"] == "held"
                    and current["lease_expires_at"]
                    and utc(current["lease_expires_at"]) > now
                ):
                    raise GenerationLeaseError(
                        "GENERATION_PROJECT_BUSY",
                        "Project orchestration is already running.",
                        current["active_request_id"],
                        current["stage"],
                    )
                if current["status"] not in {"held", "free"} or (
                    current["status"] == "held" and not current["lease_expires_at"]
                ):
                    raise GenerationLeaseError(
                        "GENERATION_LEASE_INVALID_STATE",
                        "Generation lease requires operator reconciliation.",
                        current["active_request_id"],
                        current["stage"],
                    )
                mission = await one(
                    connection,
                    "SELECT request_id,status FROM agent_missions WHERE project_id=%s AND active_slot=1",
                    (project,),
                )
                if (
                    mission
                    and mission["request_id"] != request
                    and not (stage == "manual_validation" and request is None)
                ):
                    raise GenerationLeaseError(
                        "GENERATION_MISSION_BUSY",
                        "Project already has an active Mission.",
                        mission["request_id"],
                        "mission",
                    )
                expiry = now + timedelta(milliseconds=lease_ttl_ms)
                changed = await one(
                    connection,
                    """UPDATE agent_generation_leases SET active_request_id=%s,operation_id=%s,stage=%s,status='held',lease_owner=%s,
                    lease_expires_at=%s,last_heartbeat_at=%s,fencing_token=fencing_token+1,version=version+1,acquired_at=%s,released_at=NULL,updated_at=%s
                    WHERE project_id=%s AND version=%s AND (status='free' OR (status='held' AND lease_expires_at<=%s)) RETURNING *""",
                    (
                        request,
                        operation,
                        stage,
                        owner,
                        expiry,
                        now,
                        now,
                        now,
                        project,
                        current["version"],
                        now,
                    ),
                )
                if not changed:
                    raise GenerationLeaseError(
                        "GENERATION_LEASE_CONFLICT", "Lease claim lost a concurrent write."
                    )
            return {
                "projectId": project,
                "operationId": operation,
                "requestId": request,
                "stage": stage,
                "leaseOwner": owner,
                "fencingToken": changed["fencing_token"],
                "leaseExpiresAt": iso(expiry),
            }
        except ForeignKeyViolation as error:
            raise GenerationLeaseError(
                "GENERATION_PROJECT_NOT_FOUND",
                "Generation project does not exist or is no longer writable.",
            ) from error

    async def heartbeat(self, fence, lease_ttl_ms):
        ttl(lease_ttl_ms)
        async with self.database.transaction() as connection:
            current = await lock_lease(connection, fence["projectId"])
            now = await database_now(connection)
            check_fence(current, fence, now)
            expiry = now + timedelta(milliseconds=lease_ttl_ms)
            await connection.execute(
                "UPDATE agent_generation_leases SET lease_expires_at=%s,last_heartbeat_at=%s,updated_at=%s WHERE project_id=%s",
                (expiry, now, now, fence["projectId"]),
            )
        return {"leaseExpiresAt": iso(expiry)}

    async def release(self, fence):
        async with self.database.transaction() as connection:
            current = await lock_lease(connection, fence["projectId"])
            now = await database_now(connection)
            check_fence(current, fence, now)
            await connection.execute(
                """UPDATE agent_generation_leases SET active_request_id=NULL,operation_id=NULL,stage=NULL,status='free',lease_owner=NULL,
                lease_expires_at=NULL,last_heartbeat_at=NULL,version=version+1,released_at=%s,updated_at=%s WHERE project_id=%s""",
                (now, now, fence["projectId"]),
            )

    async def fenced(self, fence, operation):
        async with self.database.transaction() as connection:
            check_fence(
                await lock_lease(connection, fence["projectId"]),
                fence,
                await database_now(connection),
            )
            result = await operation(connection)
            check_fence(
                await lock_lease(connection, fence["projectId"]),
                fence,
                await database_now(connection),
            )
            return result
