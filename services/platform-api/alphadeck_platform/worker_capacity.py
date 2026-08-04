import os
import re
import socket
from datetime import datetime, timedelta
from uuid import uuid4

from psycopg.errors import UniqueViolation

from .generation_lease import database_now
from .messages import js_length
from .quota import advisory, one
from .sessions import iso


class WorkerError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def validate(pool, capacity, owner, ttl, *, registry=False):
    code = "WORKER_REGISTRY_INVALID" if registry else "WORKER_POOL_INVALID"
    if not isinstance(pool, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,127}", pool):
        raise WorkerError(code, "Worker pool key must be a bounded namespaced identifier.")
    if type(capacity) is not int or not 1 <= capacity <= 256:
        raise WorkerError(code, "Worker global capacity must be between 1 and 256.")
    if not isinstance(owner, str) or not owner or js_length(owner) > 256:
        raise WorkerError(code, "Worker owner identity must be a bounded string.")
    if type(ttl) is not int or not 1000 <= ttl <= 86400000:
        raise WorkerError(code, "Worker lease TTL must be between one second and one day.")


def registry_claim(row):
    return {
        "id": str(row["id"]),
        "poolKey": row["pool_key"],
        "hostname": row["hostname"],
        "processId": row["process_id"],
        "processConcurrency": row["process_concurrency"],
        "globalConcurrency": row["global_concurrency"],
        **{
            key: iso(row[column])
            for key, column in (
                ("leaseExpiresAt", "lease_expires_at"),
                ("lastHeartbeatAt", "last_heartbeat_at"),
                ("startedAt", "started_at"),
            )
        },
    }


class WorkerRegistry:
    def __init__(self, database):
        self.database = database

    async def register(
        self,
        process_concurrency,
        global_concurrency,
        ttl,
        *,
        pool="generation.default",
        identity=None,
        hostname=None,
        process_id=None,
        retention_ms=604800000,
    ):
        identity, hostname, process_id = (
            identity or str(uuid4()),
            hostname or socket.gethostname(),
            process_id or os.getpid(),
        )
        validate(pool, global_concurrency, identity, ttl, registry=True)
        if (
            js_length(identity) > 128
            or not hostname
            or js_length(hostname) > 255
            or type(process_id) is not int
            or not 1 <= process_id <= 9007199254740991
            or type(process_concurrency) is not int
            or not 1 <= process_concurrency <= min(16, global_concurrency)
            or type(retention_ms) is not int
            or not 0 <= retention_ms <= 9007199254740991
        ):
            raise WorkerError("WORKER_REGISTRY_INVALID", "Invalid worker process configuration.")
        async with self.database.transaction() as tx:
            await advisory(tx, "agent-worker-pool", pool)
            now = await database_now(tx)
            await tx.execute(
                "UPDATE agent_worker_instances SET status='stale',lease_expires_at=NULL,stopped_at=%s,updated_at=%s WHERE pool_key=%s AND status='running' AND lease_expires_at<=%s",
                (now, now, pool, now),
            )
            if retention_ms:
                await tx.execute(
                    "DELETE FROM agent_worker_instances WHERE pool_key=%s AND status IN ('stale','stopped') AND stopped_at<%s",
                    (pool, now - timedelta(milliseconds=retention_ms)),
                )
            mismatch = await one(
                tx,
                "SELECT id,hostname,process_id,global_concurrency FROM agent_worker_instances WHERE id<>%s AND pool_key=%s AND status='running' AND lease_expires_at>%s AND global_concurrency<>%s LIMIT 1",
                (identity, pool, now, global_concurrency),
            )
            if mismatch:
                raise WorkerError(
                    "WORKER_POOL_CONFIG_MISMATCH",
                    f"Worker pool {pool} already has a live instance configured for global concurrency {mismatch['global_concurrency']}; refusing {global_concurrency}.",
                )
            row = await one(
                tx,
                """INSERT INTO agent_worker_instances (id,pool_key,hostname,process_id,process_concurrency,global_concurrency,status,lease_expires_at,last_heartbeat_at,started_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,'running',%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET pool_key=excluded.pool_key,hostname=excluded.hostname,process_id=excluded.process_id,
                process_concurrency=excluded.process_concurrency,global_concurrency=excluded.global_concurrency,status='running',
                lease_expires_at=excluded.lease_expires_at,last_heartbeat_at=excluded.last_heartbeat_at,started_at=excluded.started_at,stopped_at=NULL,updated_at=excluded.updated_at RETURNING *""",
                (
                    identity,
                    pool,
                    hostname,
                    process_id,
                    process_concurrency,
                    global_concurrency,
                    now + timedelta(milliseconds=ttl),
                    now,
                    now,
                    now,
                ),
            )
            return registry_claim(row)

    async def heartbeat(self, claim, ttl):
        validate(claim["poolKey"], claim["globalConcurrency"], claim["id"], ttl, registry=True)
        async with self.database.transaction() as tx:
            now = await database_now(tx)
            row = await one(
                tx,
                """UPDATE agent_worker_instances SET lease_expires_at=%s,last_heartbeat_at=%s,updated_at=%s
                WHERE id=%s AND pool_key=%s AND status='running' AND process_concurrency=%s AND global_concurrency=%s
                AND lease_expires_at>%s AND started_at=%s RETURNING lease_expires_at""",
                (
                    now + timedelta(milliseconds=ttl),
                    now,
                    now,
                    claim["id"],
                    claim["poolKey"],
                    claim["processConcurrency"],
                    claim["globalConcurrency"],
                    now,
                    datetime.fromisoformat(claim["startedAt"].replace("Z", "+00:00")),
                ),
            )
            if not row:
                raise WorkerError(
                    "WORKER_INSTANCE_LEASE_LOST", "Worker process registration lease was lost."
                )
            return {"leaseExpiresAt": iso(row["lease_expires_at"])}

    async def stop(self, claim):
        async with self.database.transaction() as tx:
            await tx.execute(
                """UPDATE agent_worker_instances SET status='stopped',lease_expires_at=NULL,stopped_at=clock_timestamp(),updated_at=clock_timestamp()
                WHERE id=%s AND pool_key=%s AND status='running' AND started_at=%s""",
                (
                    claim["id"],
                    claim["poolKey"],
                    datetime.fromisoformat(claim["startedAt"].replace("Z", "+00:00")),
                ),
            )


class WorkerCapacity:
    def __init__(self, database):
        self.database = database

    async def claim(self, capacity, job, owner, ttl, *, pool="generation.default"):
        validate(pool, capacity, owner, ttl)
        if not isinstance(job, str) or not job:
            raise WorkerError("WORKER_POOL_INVALID", "Worker slot job identity is required.")
        try:
            async with self.database.transaction() as tx:
                now = await database_now(tx)
                # Serialize only initialization, before SKIP LOCKED selection; existing rows are untouched.
                await tx.execute(
                    "INSERT INTO agent_worker_slots (pool_key,slot_number,updated_at) SELECT %s,n,now() FROM generate_series(1,%s) n ON CONFLICT DO NOTHING",
                    (pool, capacity),
                )
                slot = await one(
                    tx,
                    """SELECT slot_number,fencing_token FROM agent_worker_slots s WHERE pool_key=%s AND slot_number<=%s
                    AND (status='free' OR (status='held' AND lease_expires_at<=%s AND NOT EXISTS
                      (SELECT 1 FROM agent_generation_jobs j WHERE j.id=s.active_job_id AND j.status='running' AND j.lease_expires_at>%s)))
                    ORDER BY slot_number FOR UPDATE SKIP LOCKED LIMIT 1""",
                    (pool, capacity, now, now),
                )
                if not slot:
                    return None
                row = await one(
                    tx,
                    """UPDATE agent_worker_slots SET status='held',active_job_id=%s,lease_owner=%s,lease_expires_at=%s,last_heartbeat_at=%s,
                    fencing_token=fencing_token+1,version=version+1,acquired_at=%s,released_at=NULL,updated_at=%s WHERE pool_key=%s AND slot_number=%s RETURNING *""",
                    (
                        job,
                        owner,
                        now + timedelta(milliseconds=ttl),
                        now,
                        now,
                        now,
                        pool,
                        slot["slot_number"],
                    ),
                )
                return {
                    "poolKey": pool,
                    "slotNumber": row["slot_number"],
                    "activeJobId": job,
                    "leaseOwner": owner,
                    "fencingToken": row["fencing_token"],
                    "leaseExpiresAt": iso(row["lease_expires_at"]),
                }
        except UniqueViolation:
            return None

    async def _mutate(self, fence, ttl=None):
        releasing = ttl is None
        if not releasing:
            validate(fence["poolKey"], fence["slotNumber"], fence["leaseOwner"], ttl)
        async with self.database.transaction() as tx:
            now = await database_now(tx)
            row = await one(
                tx,
                """UPDATE agent_worker_slots SET status=CASE WHEN %s THEN 'free' ELSE 'held' END,
                active_job_id=CASE WHEN %s THEN NULL ELSE active_job_id END,lease_owner=CASE WHEN %s THEN NULL ELSE lease_owner END,
                lease_expires_at=%s,last_heartbeat_at=%s,version=version+1,released_at=CASE WHEN %s THEN %s ELSE released_at END,updated_at=%s
                WHERE pool_key=%s AND slot_number=%s AND status='held' AND active_job_id=%s AND lease_owner=%s AND fencing_token=%s AND lease_expires_at>%s RETURNING lease_expires_at""",
                (
                    releasing,
                    releasing,
                    releasing,
                    None if releasing else now + timedelta(milliseconds=ttl),
                    None if releasing else now,
                    releasing,
                    now,
                    now,
                    fence["poolKey"],
                    fence["slotNumber"],
                    fence["activeJobId"],
                    fence["leaseOwner"],
                    fence["fencingToken"],
                    now,
                ),
            )
            if not row:
                raise WorkerError(
                    "WORKER_SLOT_LEASE_LOST", "Worker no longer owns the global execution slot."
                )
            return {"leaseExpiresAt": iso(row["lease_expires_at"])} if not releasing else None

    async def heartbeat(self, fence, ttl):
        return await self._mutate(fence, ttl)

    async def release(self, fence):
        await self._mutate(fence)
