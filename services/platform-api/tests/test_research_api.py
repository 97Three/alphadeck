import asyncio
from uuid import uuid4

import httpx
from test_projects import client_for, project  # noqa: F401

from alphadeck_platform.auth import Authorization
from alphadeck_platform.research_reports import CATALOG, ResearchReports
from app import create_app


def test_research_persistence_metered_replay_and_delivery_fences(project, actor):  # noqa: F811
    async def run():
        database, calls, sent = project["database"], [], []
        watchlist_id, channel_id = "watchlist-" + uuid4().hex, "channel-" + uuid4().hex

        def market_handler(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/universes/summary"):
                value = {
                    "universes": [
                        {"id": CATALOG["universeId"], "ready_count": 8, "member_count": 10}
                    ]
                }
            elif "short-term-candidates" in request.url.path:
                value = {
                    "candidates": [{"symbol": "600519", "name": "贵州茅台", "score": 88.5}],
                    "analytics": {"engine": "timescaledb"},
                }
            else:
                return httpx.Response(503, text="controlled unavailable analytics")
            return httpx.Response(200, json=value)

        async def notification_handler(request):
            sent.append(request)
            await asyncio.sleep(0.02)
            return httpx.Response(200, json={"errcode": 0})

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(market_handler)) as market,
            httpx.AsyncClient(transport=httpx.MockTransport(notification_handler)) as notifications,
        ):
            service = ResearchReports(
                database, market_client=market, notification_client=notifications
            )
            await service.seed()
            async with database.transaction() as tx:
                await tx.execute(
                    "UPDATE auth_users SET role='admin' WHERE id=%s", (actor["user_id"],)
                )
                await tx.execute(
                    """INSERT INTO notification_channels(id,name,channel_type,config,is_dry_run,updated_at)
                    VALUES (%s,'Controlled notification','wxwork','{"webhookUrl":"https://fixture.invalid/webhook"}',false,now())""",
                    (channel_id,),
                )
                await tx.execute(
                    """INSERT INTO research_watchlists
                    SELECT %s,name,description,universe_id,symbols,markets,status,schedule,report_template,jsonb_build_array(%s::text),metadata,created_at,updated_at
                    FROM research_watchlists WHERE id=%s""",
                    (watchlist_id, channel_id, CATALOG["watchlistId"]),
                )
            project["app"] = create_app(
                database,
                Authorization(database, enabled=True, secret="test-secret"),
                projects_dir=project["root"],
                market_client=market,
                notification_client=notifications,
            )
            keys = []
            try:
                async with client_for(project) as client:
                    key = "report-run-" + uuid4().hex
                    body = {"watchlistId": watchlist_id, "idempotencyKey": key}
                    first = await client.post("/api/research/reports", json=body)
                    assert first.status_code == 201, first.text
                    report = next(
                        row
                        for row in first.json()["data"]["latestReports"]
                        if row["watchlistId"] == watchlist_id
                    )
                    assert "贵州茅台" in report["contentMarkdown"]
                    assert report["evidence"][2]["status"] == "unavailable"
                    assert sent == [] and len(calls) == 3
                    replay = await client.post("/api/research/reports", json=body)
                    assert (
                        replay.headers["idempotency-replayed"] == "true"
                        and replay.json() == first.json()
                    )
                    assert len(calls) == 3
                    send = {"action": "send-latest-report", "reportId": report["id"]}
                    assert (
                        await client.post("/api/research/reports", json=send)
                    ).status_code == 400
                    assert (
                        await client.post("/api/research/reports", json={**send, "confirmed": True})
                    ).status_code == 400
                    dry = await client.post("/api/research/reports", json={**send, "dryRun": True})
                    assert dry.status_code == 201 and sent == []
                    identity = "delivery-" + uuid4().hex
                    keys.append(identity)
                    results = await asyncio.gather(
                        *(
                            service.send(report_id=report["id"], idempotency_key=identity)
                            for _ in range(4)
                        )
                    )
                    assert len(sent) == 1
                    assert all(result["summary"]["reports"] >= 1 for result in results)
                    # Even a failed global reservation cannot resend an already reserved channel.
                    async with database.transaction() as tx:
                        await tx.execute(
                            "UPDATE platform_settings SET value=jsonb_set(value,'{status}','\"failed\"') WHERE key LIKE 'research-delivery-idempotency:%%' AND value->>'reportId'=%s",
                            (report["id"],),
                        )
                    await service.send(report_id=report["id"], idempotency_key=identity)
                    assert len(sent) == 1
                    dashboard = await client.get("/api/research/reports")
                    assert dashboard.status_code == 200
                    assert all(
                        "config" not in channel
                        for channel in dashboard.json()["data"]["notificationChannels"]
                    )
                    operations = await database.fetch_all(
                        "SELECT * FROM api_idempotency_operations WHERE actor_key=%s AND scope='research-reports'",
                        (actor["user_id"],),
                    )
                    assert any(row["quota_accounted_at"] is not None for row in operations)
            finally:
                async with database.transaction() as tx:
                    await tx.execute(
                        "DELETE FROM platform_settings WHERE key LIKE 'research-delivery-idempotency:%%' AND value->>'reportId' IN (SELECT id FROM research_reports WHERE watchlist_id=%s)",
                        (watchlist_id,),
                    )
                    await tx.execute(
                        "DELETE FROM notification_deliveries WHERE channel_id=%s", (channel_id,)
                    )
                    await tx.execute(
                        "DELETE FROM research_report_runs WHERE watchlist_id=%s", (watchlist_id,)
                    )
                    await tx.execute("DELETE FROM research_watchlists WHERE id=%s", (watchlist_id,))
                    await tx.execute("DELETE FROM notification_channels WHERE id=%s", (channel_id,))

    asyncio.run(run())
