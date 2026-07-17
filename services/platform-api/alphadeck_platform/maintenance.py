import logging

from .api_idempotency import ApiOperations
from .quota import Quota


async def reconcile_run_usage(database, *, limit=200):
    rows = await database.fetch_all(
        """SELECT r.* FROM agent_runs r WHERE r.actor_user_id IS NOT NULL AND r.total_tokens>0
        AND r.status IN ('candidate_complete','completed','failed','cancelled','timed_out','interrupted')
        AND NOT EXISTS (SELECT 1 FROM usage_events e WHERE e.idempotency_key='agent-run:'||r.id||':total-tokens')
        ORDER BY r.finished_at,r.created_at LIMIT %s""",
        (max(1, min(1000, limit)),),
    )
    restored = failed = 0
    quota = Quota(database)
    for row in rows:
        try:
            await quota.record_usage(
                row["actor_user_id"],
                "llm.total_tokens.monthly",
                row["total_tokens"],
                "agent-run:" + row["id"] + ":total-tokens",
                "agent_run",
                source_id=row["id"],
                project_id=row["project_id"],
                at=row["finished_at"] or row["created_at"],
                metadata={"provider": row["provider"], "model": row["model"]},
            )
            restored += 1
        except Exception:
            failed += 1
            logging.exception("Failed to reconcile durable Agent usage for run %s", row["id"])
    return {"scanned": len(rows), "reconciled": restored, "failed": failed}


async def reconcile_usage(database):
    expired = await Quota(database).cleanup_expired()
    operations = await ApiOperations(database).reconcile(limit=200)
    runs = await reconcile_run_usage(database)
    return {"expiredReservations": expired, "apiOperations": operations, "agentRuns": runs}
