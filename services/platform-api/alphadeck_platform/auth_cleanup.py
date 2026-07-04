import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta

from psycopg import sql

from .api_idempotency import ApiOperations
from .config import Settings
from .credentials import AuthOptions
from .database import Database
from .maintenance import reconcile_run_usage
from .quota import Quota


async def cleanup(database, *, dry_run=False, options=None, now=None):
    local = (options or AuthOptions()).local
    now = now or datetime.now(UTC)
    grace = local["retention"]["expiredRecordGraceSeconds"]
    conditions = {
        "sessions": ("auth_sessions", "expires_at < %s", now - timedelta(seconds=grace)),
        "verifications": ("auth_verifications", "expires_at < %s", now - timedelta(seconds=grace)),
        "rateLimits": (
            "auth_rate_limits",
            "last_request < %s",
            int(now.timestamp() * 1000)
            - (
                max(local["rateLimit"]["windowSeconds"], local["rateLimit"]["signInWindowSeconds"])
                + grace
            )
            * 1000,
        ),
        "auditEvents": (
            "auth_audit_events",
            "created_at < %s",
            now - timedelta(days=local["retention"]["auditDays"]),
        ),
    }
    result = {"dryRun": dry_run}
    if not dry_run:
        result["quotaReservations"] = await Quota(database).cleanup_expired()
        result["apiOperationQuotaSettlements"] = await ApiOperations(database).reconcile()
        result["apiOperations"] = await ApiOperations(database).cleanup(now=now)
        result["agentRunUsageEvents"] = 0
        while True:
            batch = await reconcile_run_usage(database, limit=500)
            result["agentRunUsageEvents"] += batch["reconciled"]
            if batch["scanned"] < 500 or batch["failed"] or not batch["reconciled"]:
                break
    async with database.transaction() as tx:
        for key, (table, condition, cutoff) in conditions.items():
            if dry_run:
                row = await (
                    await tx.execute(
                        sql.SQL("SELECT count(*) AS n FROM {} WHERE " + condition).format(
                            sql.Identifier(table)
                        ),
                        (cutoff,),
                    )
                ).fetchone()
                result[key] = row["n"]
            else:
                deleted = await tx.execute(
                    sql.SQL("DELETE FROM {} WHERE " + condition).format(sql.Identifier(table)),
                    (cutoff,),
                )
                result[key] = deleted.rowcount
        if dry_run:
            for key, query in (
                (
                    "quotaReservations",
                    "SELECT count(*) AS n FROM quota_reservations WHERE status='active' AND expires_at<=clock_timestamp()",
                ),
                (
                    "pendingApiOperationQuotaSettlements",
                    "SELECT count(*) AS n FROM api_idempotency_operations WHERE status='completed' AND quota_accounted_at IS NULL AND quota_settlement IS NOT NULL",
                ),
                (
                    "apiOperations",
                    "SELECT count(*) AS n FROM api_idempotency_operations WHERE retention_expires_at<=clock_timestamp() AND (status='failed' OR (status='completed' AND (quota_accounted_at IS NOT NULL OR quota_settlement IS NULL)) OR (status='running' AND lease_expires_at<=clock_timestamp()))",
                ),
                (
                    "agentRunUsageEvents",
                    "SELECT count(*) AS n FROM agent_runs r WHERE actor_user_id IS NOT NULL AND finished_at IS NOT NULL AND total_tokens>0 AND NOT EXISTS (SELECT 1 FROM usage_events e WHERE e.idempotency_key='agent-run:'||r.id||':total-tokens')",
                ),
            ):
                result[key] = (await (await tx.execute(query)).fetchone())["n"]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(cleanup(Database(Settings.from_env().database_url), dry_run=args.dry_run)),
            default=str,
        )
    )
