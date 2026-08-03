import os

from .api_errors import ApiError
from .quota import advisory, one, structural_capacity
from .runtime_store import insert

ACTIVE = ["pending", "processing", "active", "running"]
TERMINAL = {"cancelled", "completed", "failed"}
UNSET = object()


class RequestBindingError(ApiError):
    pass


def check_binding(row, project, actor=UNSET):
    if not row:
        return False
    if row["project_id"] != project:
        raise RequestBindingError(
            409, "REQUEST_PROJECT_MISMATCH", "Request ID belongs to a different project"
        )
    if actor is not UNSET and row["actor_user_id"] != actor:
        raise RequestBindingError(
            409, "REQUEST_ACTOR_MISMATCH", "The request ID is already bound to a different user."
        )
    return True


def configured_ms(name, default):
    try:
        value = int(os.getenv(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default


class UserRequests:
    def __init__(self, database):
        self.database = database

    async def binding(self, project, request, actor=UNSET):
        row = await self.database.fetch_one(
            "SELECT project_id,actor_user_id FROM user_requests WHERE id=%s", (request,)
        )
        return check_binding(row, project, actor)

    async def create(self, identity, project, instruction, *, actor=None, cli=UNSET, upsert=False):
        async with self.database.transaction() as tx:
            if actor and not upsert:
                user = await one(tx, "SELECT id FROM auth_users WHERE id=%s FOR UPDATE", (actor,))
                if not user:
                    raise ApiError(
                        409, "REQUEST_ACTOR_NOT_FOUND", "The request actor no longer exists."
                    )
            await advisory(tx, "user-request", identity)
            existing = await one(
                tx, "SELECT * FROM user_requests WHERE id=%s FOR UPDATE", (identity,)
            )
            if check_binding(existing, project, actor):
                if not upsert:
                    raise RequestBindingError(
                        409, "REQUEST_ALREADY_EXISTS", "The request ID is already in use."
                    )
                # UserRequest has no updated_at column.
                return await one(
                    tx,
                    "UPDATE user_requests SET instruction=%s,cli_preference=CASE WHEN %s THEN %s ELSE cli_preference END WHERE id=%s RETURNING *",
                    (instruction, cli is not UNSET, None if cli is UNSET else cli, identity),
                )
            if actor and not upsert:
                count = await one(
                    tx,
                    """SELECT count(*) AS n FROM user_requests r LEFT JOIN agent_generation_jobs j ON j.request_id=r.id
                    WHERE r.actor_user_id=%s AND r.status=ANY(%s) AND (j.id IS NULL OR j.status IN ('pending','retry_wait'))""",
                    (actor, ACTIVE),
                )
                await structural_capacity(tx, actor, "agent.pending", count["n"])
            return await insert(
                tx,
                "user_requests",
                {
                    "id": identity,
                    "project_id": project,
                    "actor_user_id": actor,
                    "instruction": instruction,
                    "status": "pending",
                    **({"cli_preference": cli} if cli is not UNSET else {}),
                },
            )

    async def status(self, project, identity, status, message=UNSET):
        if status not in set(ACTIVE) | TERMINAL:
            raise ValueError("Unknown user request status")
        async with self.database.transaction() as tx:
            result = await tx.execute(
                """UPDATE user_requests SET status=%s,completed_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                error_message=CASE WHEN %s THEN %s WHEN %s<>'failed' THEN NULL ELSE error_message END
                WHERE project_id=%s AND id=%s AND status=ANY(%s)""",
                (
                    status,
                    status in TERMINAL,
                    message is not UNSET,
                    None if message is UNSET else message,
                    status,
                    project,
                    identity,
                    ACTIVE,
                ),
            )
            return result.rowcount > 0

    async def cancelled(self, project, identity):
        row = await self.database.fetch_one(
            "SELECT status FROM user_requests WHERE id=%s AND project_id=%s", (identity, project)
        )
        return bool(row and row["status"] == "cancelled")

    async def active(self, project):
        stale = configured_ms("ALPHADECK_ACTIVE_REQUEST_STALE_MS", 1800000)
        orphan = configured_ms("ALPHADECK_ACTIVE_REQUEST_ORPHAN_MS", 480000)
        async with self.database.transaction() as tx:
            # Database leases remain authoritative across separate HTTP and worker processes.
            await tx.execute(
                """UPDATE user_requests r SET status='failed',completed_at=clock_timestamp(),
                error_message=CASE WHEN r.created_at<clock_timestamp()-(%s*interval '1 millisecond')
                  THEN '请求超过平台活动窗口未结束，已自动标记为失败。请重新发起任务。'
                  ELSE '请求执行中长时间没有新事件，已自动标记为失败。请重新发起任务。' END
                WHERE r.project_id=%s AND r.status=ANY(%s)
                AND (r.created_at<clock_timestamp()-(%s*interval '1 millisecond') OR
                  COALESCE((SELECT max(m.created_at) FROM messages m WHERE m.project_id=r.project_id AND m.request_id=r.id),r.created_at)<clock_timestamp()-(%s*interval '1 millisecond'))
                AND NOT EXISTS (SELECT 1 FROM agent_runs a WHERE a.project_id=r.project_id AND a.request_id=r.id AND a.status IN ('pending','running','waiting','reconciling') AND a.lease_expires_at>clock_timestamp())
                AND NOT EXISTS (SELECT 1 FROM agent_generation_jobs j WHERE j.request_id=r.id AND j.status IN ('pending','retry_wait','running') AND (j.status<>'running' OR j.lease_expires_at>clock_timestamp()))
                AND NOT EXISTS (SELECT 1 FROM agent_generation_leases l WHERE l.project_id=r.project_id AND l.active_request_id=r.id AND l.status='held' AND l.lease_expires_at>clock_timestamp())
                AND NOT EXISTS (SELECT 1 FROM agent_missions m WHERE m.project_id=r.project_id AND m.request_id=r.id AND m.status='verifying' AND m.verification_lease_expires_at>clock_timestamp())""",
                (stale, project, ACTIVE, stale, orphan),
            )
            count = (
                await one(
                    tx,
                    "SELECT count(*) AS n FROM user_requests WHERE project_id=%s AND status=ANY(%s)",
                    (project, ACTIVE),
                )
            )["n"]
            return {"hasActiveRequests": count > 0, "activeCount": count}

    async def cancel(self, project, request=None, reason="用户暂停了当前任务"):
        """Fence durable runs before returning; late worker callbacks cannot regain authority."""
        from .dispatch import Dispatch

        async with self.database.transaction() as tx:
            await one(
                tx,
                "SELECT project_id FROM agent_workspace_leases WHERE project_id=%s FOR UPDATE",
                (project,),
            )
            await (
                await tx.execute(
                    "SELECT id FROM agent_missions WHERE project_id=%s AND (%s::text IS NULL OR request_id=%s) ORDER BY id FOR UPDATE",
                    (project, request, request),
                )
            ).fetchall()
            if request:
                row = await one(tx, "SELECT * FROM user_requests WHERE id=%s", (request,))
                if not check_binding(row, project):
                    raise ApiError(404, "REQUEST_NOT_FOUND", "Request not found for this project")
            rows = await (
                await tx.execute(
                    "SELECT id FROM agent_runs WHERE project_id=%s AND (%s::text IS NULL OR request_id=%s) AND status IN ('pending','running','waiting','reconciling') FOR UPDATE",
                    (project, request, request),
                )
            ).fetchall()
            identities = [row["id"] for row in rows]
            if identities:
                await tx.execute(
                    """UPDATE agent_runs SET status='cancelled',finished_at=clock_timestamp(),lease_owner=NULL,lease_expires_at=NULL,
                    fencing_token=fencing_token+1,version=version+1,error_code='USER_CANCELLED',error_message=%s,updated_at=clock_timestamp() WHERE id=ANY(%s)""",
                    (reason, identities),
                )
                await tx.execute(
                    """UPDATE agent_workspace_leases SET status='free',active_run_id=NULL,lease_owner=NULL,lease_expires_at=NULL,
                    released_at=clock_timestamp(),fencing_token=fencing_token+1,version=version+1,updated_at=clock_timestamp() WHERE project_id=%s AND active_run_id=ANY(%s)""",
                    (project, identities),
                )
                await tx.execute(
                    "UPDATE agent_tool_approvals SET status='expired',resolved_at=clock_timestamp(),updated_at=clock_timestamp() WHERE run_id=ANY(%s) AND status='pending'",
                    (identities,),
                )
            cancelled = await (
                await tx.execute(
                    "UPDATE user_requests SET status='cancelled',completed_at=clock_timestamp(),error_message=%s WHERE project_id=%s AND (%s::text IS NULL OR id=%s) AND status=ANY(%s) RETURNING id",
                    (reason, project, request, request, ACTIVE),
                )
            ).fetchall()
            missions = await (
                await tx.execute(
                    """UPDATE agent_missions SET status='cancelled',active_slot=NULL,error_code='MISSION_CANCELLED',error_message=%s,
                verification_lease_owner=NULL,verification_lease_expires_at=NULL,verification_last_heartbeat_at=NULL,
                completed_at=clock_timestamp(),version=version+1,updated_at=clock_timestamp()
                WHERE project_id=%s AND (%s::text IS NULL OR request_id=%s) AND status NOT IN ('completed','failed','cancelled') RETURNING id""",
                    (reason[:2000], project, request, request),
                )
            ).fetchall()
            if missions:
                await tx.execute(
                    "UPDATE agent_mission_nodes SET status='failed',version=version+1,finished_at=clock_timestamp(),updated_at=clock_timestamp() WHERE mission_id=ANY(%s) AND status IN ('pending','running')",
                    ([row["id"] for row in missions],),
                )
            remaining = (
                await one(
                    tx,
                    "SELECT count(*) AS n FROM agent_runs WHERE project_id=%s AND status IN ('pending','running','waiting','reconciling')",
                    (project,),
                )
            )["n"]
        for identity in [request] if request else [row["id"] for row in cancelled]:
            await Dispatch(self.database).cancel(project, identity, reason)
        return {
            "cancelled": len(identities),
            "activeCount": remaining,
            "cancelledMissions": len(missions),
        }
