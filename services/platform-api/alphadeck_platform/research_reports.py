"""Persist evidence based reports and reserve notification deliveries before contacting adapters."""

import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
from psycopg.types.json import Jsonb

from .config import ROOT, market_base_url
from .generation_state import now
from .messages import js_slice
from .research_notifications import deliver_notification
from .research_report_content import build_report, evidence_array, probe_metrics, provider_matrix
from .service_catalog import degradation_config
from .sessions import iso
from .skill_source import strict_json
from .strategy_mappers import record

CATALOG = json.loads((ROOT / "config/research-report-catalog.json").read_text())
FIELDS = {
    "watchlists": "id name description universe_id symbols markets status schedule report_template notification_channel_ids updated_at",
    "runs": "id watchlist_id status run_type started_at finished_at provider_mode error",
    "reports": "id run_id watchlist_id title summary report_date score recommendation risk_level content_markdown structured evidence source created_at",
    "channels": "id name channel_type status target is_dry_run updated_at",
    "deliveries": "id run_id report_id channel_id channel_type status title error delivered_at created_at",
}


def camel(value):
    first, *rest = value.split("_")
    return first + "".join(part.title() for part in rest)


def view(row, kind):
    result = {}
    for key in FIELDS[kind].split():
        value = row[key]
        if key.endswith("_at") or key == "report_date":
            value = iso(value) if value is not None else None
        elif key in ("symbols", "markets", "notification_channel_ids"):
            value = (
                [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
            )
        elif key in ("schedule", "structured"):
            value = record(value)
        elif key == "evidence":
            value = evidence_array(value)
        result[camel(key)] = value
    return result


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ResearchReports:
    def __init__(self, database, *, market_client=None, notification_client=None):
        self.database = database
        self.market_client, self.notification_client = market_client, notification_client

    async def seed(self):
        async with self.database.transaction() as tx:
            await tx.execute(
                """INSERT INTO notification_channels
                (id,name,channel_type,target,config,is_dry_run,updated_at)
                VALUES (%s,%s,'wxwork',%s,%s,true,now()) ON CONFLICT(id) DO NOTHING""",
                (
                    CATALOG["channelId"],
                    "企业微信投研日报 Dry-run",
                    "本地模拟推送",
                    Jsonb(
                        {
                            "mode": "dry_run",
                            "adapter": "wxwork-webhook",
                            "webhookEnv": "ALPHADECK_WXWORK_RESEARCH_WEBHOOK",
                            "secretConfigured": False,
                        }
                    ),
                ),
            )
            await tx.execute(
                """INSERT INTO research_watchlists
                (id,name,description,universe_id,symbols,markets,schedule,notification_channel_ids,metadata,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT(id) DO NOTHING""",
                (
                    CATALOG["watchlistId"],
                    "AlphaDeck 每日核心观察池",
                    "围绕默认 A 股研究池和少量核心标的生成证据型日报。",
                    CATALOG["universeId"],
                    Jsonb(CATALOG["symbols"]),
                    Jsonb(CATALOG["markets"]),
                    Jsonb(
                        {
                            "timezone": "Asia/Shanghai",
                            "frequency": "daily",
                            "time": "08:30",
                            "enabled": False,
                        }
                    ),
                    Jsonb([CATALOG["channelId"]]),
                    Jsonb(
                        {
                            "seeded": True,
                            "intent": "research_automation_p0",
                            "sourceProject": "AlphaDeck",
                        }
                    ),
                ),
            )

    async def dashboard(self):
        await self.seed()
        tables = (
            ("watchlists", "research_watchlists", "updated_at", 20),
            ("reports", "research_reports", "created_at", 20),
            ("runs", "research_report_runs", "started_at", 20),
            ("channels", "notification_channels", "updated_at", 10),
            ("deliveries", "notification_deliveries", "created_at", 20),
        )
        # These identifiers come only from the static tuple above.
        rows = await asyncio.gather(
            *(
                self.database.fetch_all(
                    f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT {limit}"
                )
                for _, table, order, limit in tables
            )
        )
        mapped = {
            kind: [view(row, kind) for row in values]
            for (kind, *_), values in zip(tables, rows, strict=True)
        }
        totals = await self.database.fetch_one("""SELECT
            (SELECT count(*) FROM research_watchlists) AS watchlists,
            (SELECT count(*) FROM research_reports) AS reports,
            (SELECT count(*) FROM notification_channels WHERE status <> 'disabled') AS channels""")
        reports, channels = mapped["reports"], mapped["channels"]
        return {
            "generatedAt": now(),
            "summary": {
                "watchlists": totals["watchlists"],
                "reports": totals["reports"],
                "activeChannels": totals["channels"],
                "latestScore": reports[0]["score"] if reports else None,
            },
            "watchlists": mapped["watchlists"],
            "latestReports": reports,
            "recentRuns": mapped["runs"],
            "notificationChannels": channels,
            "recentDeliveries": mapped["deliveries"],
            "providerMatrix": provider_matrix(reports[0]["evidence"] if reports else [], channels),
        }

    async def probe(self, path, source, timeout):
        evidence = {"source": source, "status": "unavailable", "detail": "", "capturedAt": now()}
        if not degradation_config()["components"]["marketApi"]["enabled"]:
            return {
                "data": None,
                "evidence": {
                    **evidence,
                    "status": "disabled",
                    "detail": "market-data API 已按降级配置停用。",
                },
            }
        owned = self.market_client is None
        client = self.market_client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        try:
            async with asyncio.timeout(timeout):
                response = await client.get(market_base_url() + path, timeout=timeout)
                if len(response.content) > 33554432:
                    raise ValueError("market-data response exceeds size limit")
                if not response.is_success:
                    evidence["detail"] = f"market-data API {response.status_code}: " + js_slice(
                        response.text, 0, 180
                    )
                    return {"data": None, "evidence": evidence}
                value = strict_json(response.content)
                return {
                    "data": value,
                    "evidence": {
                        **evidence,
                        "status": "available",
                        "detail": "已读取本地 market-data API。",
                        "metrics": probe_metrics(value),
                    },
                }
        except (httpx.HTTPError, ValueError, TimeoutError) as error:
            return {
                "data": None,
                "evidence": {**evidence, "detail": str(error) or "market-data request timed out"},
            }
        finally:
            if owned:
                await client.aclose()

    async def channels(self, watchlist_id):
        watchlist = (
            await self.database.fetch_one(
                "SELECT notification_channel_ids FROM research_watchlists WHERE id=%s",
                (watchlist_id,),
            )
            if watchlist_id
            else None
        )
        identifiers = watchlist["notification_channel_ids"] if watchlist else [CATALOG["channelId"]]
        rows = await self.database.fetch_all(
            "SELECT * FROM notification_channels WHERE id=ANY(%s) AND status <> 'disabled'",
            (identifiers,),
        )
        if not rows:
            rows = await self.database.fetch_all(
                "SELECT * FROM notification_channels WHERE id=%s", (CATALOG["channelId"],)
            )
        return [{**view(row, "channels"), "config": row["config"]} for row in rows]

    async def deliveries(self, run_id, report, channels, *, dry_run, key=None):
        result = []
        for channel in channels:
            identity = (
                "research-notification:" + digest(key + "\0" + report["id"] + "\0" + channel["id"])
                if key
                else None
            )
            async with self.database.transaction() as tx:
                reserved = await (
                    await tx.execute(
                        """INSERT INTO notification_deliveries
                    (id,idempotency_key,run_id,report_id,channel_id,status,channel_type,title,payload,updated_at)
                    VALUES (%s,%s,%s,%s,%s,'sending',%s,%s,%s,now())
                    ON CONFLICT(idempotency_key) DO NOTHING RETURNING *""",
                        (
                            str(uuid4()),
                            identity,
                            run_id,
                            report["id"],
                            channel["id"],
                            channel["channelType"],
                            report["title"],
                            Jsonb(
                                {
                                    "mode": "dry_run_reserved"
                                    if dry_run or channel["isDryRun"]
                                    else "real_reserved"
                                }
                            ),
                        ),
                    )
                ).fetchone()
                if reserved is None:
                    previous = await (
                        await tx.execute(
                            "SELECT * FROM notification_deliveries WHERE idempotency_key=%s",
                            (identity,),
                        )
                    ).fetchone()
                    result.append(view(previous, "deliveries"))
                    continue
            # Commit the reservation before I/O. An uncertain attempt must never be sent twice.
            try:
                delivered = await deliver_notification(
                    channel, report, force_dry_run=dry_run, client=self.notification_client
                )
                async with self.database.transaction() as tx:
                    row = await (
                        await tx.execute(
                            """UPDATE notification_deliveries SET status=%s,payload=%s,error=%s,
                        delivered_at=%s,updated_at=now() WHERE id=%s RETURNING *""",
                            (
                                delivered["status"],
                                Jsonb(delivered["payload"]),
                                delivered["error"],
                                delivered["deliveredAt"],
                                reserved["id"],
                            ),
                        )
                    ).fetchone()
            except Exception as error:
                row = reserved
                try:
                    async with self.database.transaction() as tx:
                        row = await (
                            await tx.execute(
                                "UPDATE notification_deliveries SET status='failed',error=%s,updated_at=now() WHERE id=%s RETURNING *",
                                (str(error), reserved["id"]),
                            )
                        ).fetchone()
                except Exception:
                    pass
            result.append(view(row, "deliveries"))
        return result

    async def run_daily(self, *, watchlist_id=None, dry_run=True, run_type=None):
        from urllib.parse import quote

        await self.seed()
        row = await self.database.fetch_one(
            "SELECT * FROM research_watchlists WHERE id=%s AND status='active'",
            (watchlist_id or CATALOG["watchlistId"],),
        )
        if row is None:
            raise ValueError("Research watchlist not found")
        watchlist, run_id, started = view(row, "watchlists"), str(uuid4()), now()
        async with self.database.transaction() as tx:
            await tx.execute(
                """INSERT INTO research_report_runs
                (id,watchlist_id,status,run_type,started_at,metadata,updated_at)
                VALUES (%s,%s,'running',%s,%s,%s,now())""",
                (
                    run_id,
                    row["id"],
                    run_type or ("dry_run" if dry_run else "manual"),
                    started,
                    Jsonb({"dryRun": dry_run, "marketApiBaseUrl": market_base_url()}),
                ),
            )
        try:
            probes = await asyncio.gather(
                self.probe("/api/v1/research/universes/summary", "本地股票池", 3.5),
                self.probe(
                    "/api/v1/research/screeners/a-share/short-term-candidates?universe_id="
                    + quote(watchlist["universeId"] or CATALOG["universeId"], safe="")
                    + "&limit=8",
                    "短线候选筛选",
                    5,
                ),
                self.probe("/api/v1/analytics/clickhouse/health", "ClickHouse 分析层", 3.5),
            )
            report, report_id = build_report(watchlist, *probes), str(uuid4())
            async with self.database.transaction() as tx:
                saved = await (
                    await tx.execute(
                        """INSERT INTO research_reports
                    (id,run_id,watchlist_id,title,summary,report_date,market_scope,score,recommendation,risk_level,content_markdown,structured,evidence,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING *""",
                        (
                            report_id,
                            run_id,
                            row["id"],
                            report["title"],
                            report["summary"],
                            started,
                            Jsonb(
                                {
                                    "universeId": watchlist["universeId"],
                                    "symbols": watchlist["symbols"],
                                    "markets": watchlist["markets"],
                                }
                            ),
                            report["score"],
                            report["recommendation"],
                            report["riskLevel"],
                            report["contentMarkdown"],
                            Jsonb(report["structured"]),
                            Jsonb(report["evidence"]),
                        ),
                    )
                ).fetchone()
            deliveries = await self.deliveries(
                run_id, view(saved, "reports"), await self.channels(row["id"]), dry_run=dry_run
            )
            async with self.database.transaction() as tx:
                await tx.execute(
                    "UPDATE research_report_runs SET status='completed',finished_at=now(),metadata=%s,updated_at=now() WHERE id=%s",
                    (
                        Jsonb(
                            {
                                "dryRun": dry_run,
                                "reportId": report_id,
                                "evidenceStatuses": [
                                    {"source": item["source"], "status": item["status"]}
                                    for item in report["evidence"]
                                ],
                                "deliveryStatuses": [
                                    {
                                        key: item[key]
                                        for key in ("id", "channelType", "status", "error")
                                    }
                                    for item in deliveries
                                ],
                            }
                        ),
                        run_id,
                    ),
                )
        except BaseException as error:
            async with self.database.transaction() as tx:
                await tx.execute(
                    "UPDATE research_report_runs SET status='failed',finished_at=now(),error=%s,updated_at=now() WHERE id=%s",
                    (str(error), run_id),
                )
            raise
        return await self.dashboard()

    async def send(self, *, report_id=None, dry_run=False, idempotency_key=None):
        await self.seed()
        row = (
            await self.database.fetch_one(
                "SELECT * FROM research_reports WHERE id=%s", (report_id,)
            )
            if report_id
            else await self.database.fetch_one(
                "SELECT * FROM research_reports ORDER BY created_at DESC LIMIT 1"
            )
        )
        if row is None:
            raise ValueError("Research report not found")
        if not dry_run and not idempotency_key:
            raise ValueError("真实推送必须提供幂等键。")
        key = "research-delivery-idempotency:" + digest(idempotency_key) if not dry_run else None
        if key:
            async with self.database.transaction() as tx:
                await tx.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
                previous = await (
                    await tx.execute(
                        "SELECT value FROM platform_settings WHERE key=%s FOR UPDATE", (key,)
                    )
                ).fetchone()
                if previous:
                    if previous["value"].get("reportId") != row["id"]:
                        raise ValueError("幂等键已用于另一份投研报告。")
                    replay = previous["value"].get("status") != "failed"
                else:
                    replay = False
                if not replay:
                    await tx.execute(
                        "INSERT INTO platform_settings(key,value,updated_at) VALUES (%s,%s,now()) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
                        (
                            key,
                            Jsonb(
                                {
                                    "status": "reserved",
                                    "reportId": row["id"],
                                    "requestedAt": now(),
                                    **({"retryOfFailedAttempt": True} if previous else {}),
                                }
                            ),
                        ),
                    )
            if replay:
                return await self.dashboard()
        try:
            deliveries = await self.deliveries(
                None,
                view(row, "reports"),
                await self.channels(row["watchlist_id"]),
                dry_run=dry_run,
                key=key,
            )
            if key:
                async with self.database.transaction() as tx:
                    await tx.execute(
                        "UPDATE platform_settings SET value=%s,updated_at=now() WHERE key=%s",
                        (
                            Jsonb(
                                {
                                    "status": "completed",
                                    "reportId": row["id"],
                                    "completedAt": now(),
                                    "deliveryIds": [item["id"] for item in deliveries],
                                }
                            ),
                            key,
                        ),
                    )
        except BaseException as error:
            if key:
                async with self.database.transaction() as tx:
                    await tx.execute(
                        "UPDATE platform_settings SET value=%s,updated_at=now() WHERE key=%s",
                        (
                            Jsonb({"status": "failed", "reportId": row["id"], "error": str(error)}),
                            key,
                        ),
                    )
            raise
        return await self.dashboard()
