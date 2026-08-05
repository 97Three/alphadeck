import os

from .evaluation_queue import date
from .generation_state import now
from .service_catalog import degradation_config
from .sessions import iso

SUMMARY_FIELDS = [
    "activeWorkers",
    "staleWorkers",
    "processCapacity",
    "globalCapacity",
    "heldSlots",
    "availableSlots",
    "expiredSlots",
    "pendingJobs",
    "retryWaitJobs",
    "runningJobs",
    "failedJobsLast24h",
    "completedJobsLast24h",
    "queuedActors",
]


def unavailable(pool, mode, error):
    return {
        "generatedAt": now(),
        "available": False,
        "error": error,
        "dispatchMode": mode,
        "poolKey": pool,
        "status": "unavailable",
        "summary": {
            **dict.fromkeys(SUMMARY_FIELDS, 0),
            "oldestQueueAgeSeconds": None,
            "configurationConsistent": False,
        },
        "workers": [],
        "slots": [],
        "alerts": [
            {
                "id": "worker-observability-unavailable",
                "severity": "failed",
                "summary": "无法读取 Worker registry、全局槽位和 generation queue。",
            }
        ],
    }


async def worker_dashboard(database, *, pool="generation.default", mode=None):
    mode = mode if mode is not None else os.getenv("PI_AGENT_DISPATCH_MODE", "worker").strip()
    if not degradation_config()["components"]["database"]["enabled"]:
        return unavailable(pool, mode, "数据库已按降级配置停用，Worker 指标未采集。")
    try:
        # One database clock makes lease status independent of the Web process clock.
        clock = date((await database.fetch_one("SELECT clock_timestamp() AS time"))["time"])
        instances = await database.fetch_all(
            "SELECT * FROM agent_worker_instances WHERE pool_key=%s ORDER BY started_at DESC LIMIT 50",
            (pool,),
        )
        slot_rows = await database.fetch_all(
            "SELECT * FROM agent_worker_slots WHERE pool_key=%s ORDER BY slot_number", (pool,)
        )
        queue = await database.fetch_one("""SELECT count(*) FILTER(WHERE j.status='pending') AS pending,
            count(*) FILTER(WHERE j.status='retry_wait') AS retry,
            count(*) FILTER(WHERE j.status='running') AS running,
            count(*) FILTER(WHERE j.status='failed' AND j.completed_at >= clock_timestamp()-interval '24 hours') AS failed,
            count(*) FILTER(WHERE j.status='completed' AND j.completed_at >= clock_timestamp()-interval '24 hours') AS completed,
            count(DISTINCT r.actor_user_id) FILTER(WHERE j.status IN ('pending','retry_wait')) AS actors,
            min(j.queued_at) FILTER(WHERE j.status IN ('pending','retry_wait')) AS oldest
            FROM agent_generation_jobs j JOIN user_requests r ON r.id=j.request_id AND r.project_id=j.project_id""")
        workers = []
        for row in instances:
            live = (
                row["status"] == "running"
                and row["lease_expires_at"]
                and date(row["lease_expires_at"]) > clock
            )
            workers.append(
                {
                    "id": str(row["id"]),
                    "hostname": row["hostname"],
                    "processId": row["process_id"],
                    "processConcurrency": row["process_concurrency"],
                    "globalConcurrency": row["global_concurrency"],
                    "status": "running"
                    if live
                    else "stopped"
                    if row["status"] == "stopped"
                    else "stale",
                    "startedAt": iso(row["started_at"]),
                    "lastHeartbeatAt": iso(row["last_heartbeat_at"]),
                    "leaseExpiresAt": iso(row["lease_expires_at"])
                    if row["lease_expires_at"]
                    else None,
                }
            )
        active = [worker for worker in workers if worker["status"] == "running"]
        capacities = {worker["globalConcurrency"] for worker in active}
        try:
            configured = int(os.getenv("PI_AGENT_WORKER_GLOBAL_CONCURRENCY", "0")) or len(slot_rows)
        except ValueError:
            configured = len(slot_rows)
        capacity = active[0]["globalConcurrency"] if active else configured
        slots = []
        for row in slot_rows:
            expired = (
                row["status"] == "held"
                and row["lease_expires_at"]
                and date(row["lease_expires_at"]) <= clock
            )
            owner = row["lease_owner"] or ""
            slots.append(
                {
                    "slotNumber": row["slot_number"],
                    "status": "expired" if expired else row["status"],
                    "activeJobId": row["active_job_id"],
                    "workerId": owner.removeprefix("worker-instance:")
                    if owner.startswith("worker-instance:")
                    else None,
                    **{
                        key: iso(row[field]) if row[field] else None
                        for key, field in (
                            ("acquiredAt", "acquired_at"),
                            ("lastHeartbeatAt", "last_heartbeat_at"),
                            ("leaseExpiresAt", "lease_expires_at"),
                        )
                    },
                }
            )
        held, expired = (
            sum(slot["status"] == "held" for slot in slots),
            sum(slot["status"] == "expired" for slot in slots),
        )
        alerts = []
        if mode == "worker" and not active:
            pending = queue["pending"] + queue["retry"] > 0
            alerts.append(
                {
                    "id": "worker-consumer-missing",
                    "severity": "failed" if pending else "warning",
                    "summary": "generation queue 中存在待执行任务，但没有存活的 Worker。"
                    if pending
                    else "当前使用 Worker 调度模式，但没有存活的 Worker 注册。",
                }
            )
        if len(capacities) > 1:
            alerts.append(
                {
                    "id": "worker-capacity-mismatch",
                    "severity": "failed",
                    "summary": "存活 Worker 的全局容量配置不一致，新的 Worker 将被拒绝注册。",
                }
            )
        if expired:
            alerts.append(
                {
                    "id": "worker-slot-expired",
                    "severity": "warning",
                    "summary": f"{expired} 个 Worker 槽位租约已过期，等待安全回收。",
                }
            )
        if queue["running"] > held and mode == "worker":
            alerts.append(
                {
                    "id": "worker-job-slot-drift",
                    "severity": "warning",
                    "summary": f"{queue['running']} 个运行中 Job 仅对应 {held} 个有效全局槽位。",
                }
            )
        return {
            "generatedAt": iso(clock),
            "available": True,
            "error": None,
            "dispatchMode": mode,
            "poolKey": pool,
            "status": "failed"
            if any(alert["severity"] == "failed" for alert in alerts)
            else "warning"
            if alerts
            else "ok",
            "summary": {
                "activeWorkers": len(active),
                "staleWorkers": sum(worker["status"] == "stale" for worker in workers),
                "processCapacity": sum(worker["processConcurrency"] for worker in active),
                "globalCapacity": max(0, capacity),
                "heldSlots": held,
                "availableSlots": max(0, capacity - held),
                "expiredSlots": expired,
                "pendingJobs": queue["pending"],
                "retryWaitJobs": queue["retry"],
                "runningJobs": queue["running"],
                "failedJobsLast24h": queue["failed"],
                "completedJobsLast24h": queue["completed"],
                "queuedActors": queue["actors"],
                "oldestQueueAgeSeconds": max(
                    0, int((clock - date(queue["oldest"])).total_seconds())
                )
                if queue["oldest"]
                else None,
                "configurationConsistent": len(capacities) <= 1,
            },
            "workers": workers,
            "slots": slots,
            "alerts": alerts,
        }
    except Exception:
        return unavailable(pool, mode, "无法读取 Worker registry、全局槽位和 generation queue。")
