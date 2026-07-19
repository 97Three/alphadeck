"""Bounded native market maintenance and database freshness gate."""

import argparse
import asyncio
import json
import math
import os
import time
from contextlib import suppress
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .config import Settings, load_environment, market_base_url
from .database import Database

ACTIVE = {"pending", "running", "paused", "pause_requested", "resume_requested"}
TERMINAL = {"completed", "partial", "failed", "stopped"}


def number(name, fallback, *, minimum=1, maximum=100000, integral=True):
    raw = os.getenv(name, "").strip()
    value = float(raw) if raw else fallback
    if (
        not math.isfinite(value)
        or not minimum <= value <= maximum
        or integral
        and value != int(value)
    ):
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return int(value) if integral else value


def estimated_date(now=None):
    now = (now or datetime.now(ZoneInfo("Asia/Shanghai"))).astimezone(ZoneInfo("Asia/Shanghai"))
    day = now.date() - timedelta(days=now.hour < 18)
    while day.weekday() > 4:
        day -= timedelta(days=1)
    return day.isoformat()


def sessions(start, end):
    if not start or not end or start >= end:
        return 0
    cursor = datetime.fromisoformat(str(start)[:10]).date()
    final = datetime.fromisoformat(str(end)[:10]).date()
    count = 0
    while cursor < final:
        cursor += timedelta(days=1)
        count += cursor.weekday() < 5
    return count


def evaluate_freshness(snapshot, *, estimated=None, max_lag_sessions=0, min_symbols=1):
    estimated = estimated or estimated_date()
    through, opened, bar = (
        snapshot.get(key) for key in ("calendarThrough", "latestOpenDate", "latestBarDate")
    )
    covered = bool(through and through >= estimated)
    expected = opened if covered and opened and opened <= estimated else estimated
    lag = sessions(bar, expected) if bar else float("inf")
    bar_covered = bool(bar and bar >= expected)
    symbols = snapshot.get("symbolsAtLatest", 0) >= min_symbols
    return dict(
        ok=covered and (bar_covered or lag <= max_lag_sessions) and symbols,
        estimatedDate=estimated,
        expectedBarDate=expected,
        calendarCovered=covered,
        calendarLagSessions=0 if covered else sessions(through, estimated),
        barCovered=bar_covered,
        barLagSessions=lag if math.isfinite(lag) else None,
        maxLagSessions=max_lag_sessions,
        minSymbols=min_symbols,
        symbolCoverageMet=symbols,
        **snapshot,
    )


async def freshness(database, *, max_lag_sessions=0, min_symbols=1):
    estimated = estimated_date()
    calendar = await database.fetch_one(
        """SELECT max(trade_date)::text AS "calendarThrough",
        max(trade_date) FILTER (WHERE is_open IS TRUE AND trade_date <= %s::date)::text AS "latestOpenDate"
        FROM quant.trading_calendars WHERE market=ANY(%s::text[]) AND session='regular' """,
        (estimated, ["CN-A", "SSE", "SZSE", "BSE"]),
    )
    bars = await database.fetch_one(
        """WITH latest AS (
        SELECT max(timezone('Asia/Shanghai',ts)::date) AS trade_date FROM quant.canonical_stock_bars
        WHERE timeframe='daily' AND adjustment='qfq' AND timezone('Asia/Shanghai',ts)::date <= %s::date)
        SELECT latest.trade_date::text AS "latestBarDate",count(DISTINCT bars.symbol)::int AS "symbolsAtLatest"
        FROM latest LEFT JOIN quant.canonical_stock_bars bars
        ON timezone('Asia/Shanghai',bars.ts)::date=latest.trade_date AND bars.timeframe='daily'
        AND bars.adjustment='qfq' GROUP BY latest.trade_date""",
        (estimated,),
    )
    return evaluate_freshness(
        {**(calendar or {}), **(bars or {})},
        estimated=estimated,
        max_lag_sessions=max_lag_sessions,
        min_symbols=min_symbols,
    )


async def maintain(options, *, client=None, today=None, database=None):
    if client is None:
        token = os.getenv("ALPHADECK_MARKET_ADMIN_TOKEN", "").strip()
        async with httpx.AsyncClient(
            base_url=market_base_url(),
            headers={"Authorization": "Bearer " + token} if token else {},
            timeout=30,
        ) as owned:
            return await maintain(options, client=owned, today=today, database=database)
    today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    lookback = number("ALPHADECK_MARKET_HISTORY_LOOKBACK_DAYS", 14, maximum=366)
    maximum = number("ALPHADECK_MARKET_MAINTENANCE_MAX_SYMBOLS", 300, maximum=10000)
    batch = min(maximum, number("ALPHADECK_MARKET_MAINTENANCE_BATCH_SIZE", 25, maximum=200))
    universe = (
        os.getenv("ALPHADECK_MARKET_MAINTENANCE_UNIVERSE_ID", "").strip()
        or "a-share-sample-research-pool"
    )
    calendar = dict(
        start=(
            today
            - timedelta(days=number("ALPHADECK_MARKET_CALENDAR_LOOKBACK_DAYS", 30, maximum=366))
        ).isoformat(),
        end=today.isoformat(),
    )
    history = dict(
        universe_id=universe,
        period="daily",
        adjustment="qfq",
        start=(today - timedelta(days=lookback)).isoformat(),
        end=today.strftime("%Y%m%d"),
        limit=max(30, lookback * 2),
        lookback_years=1,
        allow_fallback=False,
        request_delay_seconds=number(
            "ALPHADECK_MARKET_MAINTENANCE_REQUEST_DELAY_SECONDS",
            0.2,
            minimum=0,
            maximum=60,
            integral=False,
        ),
        batch_delay_seconds=number(
            "ALPHADECK_MARKET_MAINTENANCE_BATCH_DELAY_SECONDS",
            0.7,
            minimum=0,
            maximum=60,
            integral=False,
        ),
        batch_size=batch,
        max_batches=maximum // batch,
        max_retries=number("ALPHADECK_MARKET_MAINTENANCE_MAX_RETRIES", 3, maximum=10),
        include_valuation_factors=False,
    )

    async def request(method, path, *, body=None, timeout=30):
        response = await client.request(method, path, json=body, timeout=timeout)
        response.raise_for_status()
        return response.json()

    scope = None
    if not options.calendar_only:
        members = await request(
            "GET", f"/api/v1/research/universes/{quote(universe, safe='')}/members?page_size=1"
        )
        total = members.get("total")
        bound = history["max_batches"] * batch
        if type(total) is not int or not 1 <= total <= bound:
            raise ValueError("Universe scope is invalid or exceeds the bounded plan")
        scope = dict(
            universeId=universe,
            totalSymbols=total,
            maxSymbols=bound,
            maxRows=bound * history["limit"],
        )
    plan = dict(
        baseUrl=market_base_url(),
        calendarBody=calendar,
        historyBody=None if options.calendar_only else history,
        scope=scope,
    )
    if options.dry_run:
        return dict(dryRun=True, **plan)
    calendar_result = await request(
        "POST",
        "/api/v1/foundation/trading-calendar/refresh",
        body=calendar,
        timeout=number("ALPHADECK_MARKET_CALENDAR_TIMEOUT_MS", 120000, maximum=900000) / 1000,
    )
    ingestion = None
    if not options.calendar_only:
        jobs_path = f"/api/v1/ingestion/jobs?universe_id={quote(universe, safe='')}&limit=100"
        jobs = await request("GET", jobs_path)
        active = next(
            (
                job
                for job in jobs.get("jobs", [])
                if job.get("provider") == "baostock-autofill" and job.get("status") in ACTIVE
            ),
            None,
        )
        if active:
            metadata = active.get("metadata") or {}
            if (
                any(
                    metadata.get(key) != history[key]
                    for key in ("start", "end", "max_batches", "batch_size")
                )
                or active.get("timeframe") != "daily"
                or active.get("adjustment") != "qfq"
                or float(active.get("total_symbols") or 0) > scope["maxSymbols"]
            ):
                raise ValueError(
                    "An active ingestion job has a different scope; wait for it to finish"
                )
            job_id = active.get("id")
        else:
            job_id = (
                await request(
                    "POST", "/api/v1/ingestion/baostock/history/autofill", body=history, timeout=120
                )
            ).get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("Baostock autofill did not return a job_id")
        timeout = number("ALPHADECK_MARKET_INGESTION_TIMEOUT_MS", 1800000, maximum=7200000) / 1000
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = await request("GET", jobs_path, timeout=min(30, timeout))
            ingestion = next((job for job in jobs.get("jobs", []) if job.get("id") == job_id), None)
            if ingestion and ingestion.get("status") in TERMINAL:
                break
            await asyncio.sleep(
                number("ALPHADECK_MARKET_MAINTENANCE_POLL_INTERVAL_MS", 2000, maximum=60000) / 1000
            )
        else:
            if not active:
                with suppress(httpx.HTTPError):
                    await request(
                        "POST",
                        f"/api/v1/ingestion/jobs/{quote(job_id, safe='')}/control",
                        body={
                            "action": "stop",
                            "reason": "AlphaDeck market maintenance timed out.",
                        },
                    )
            raise TimeoutError(f"Market ingestion job {job_id} timed out")
        if (
            ingestion.get("status") != "completed"
            or float(ingestion.get("completed_symbols") or 0) <= 0
            or float(ingestion.get("failed_symbols") or 0) > 0
        ):
            raise ValueError("Daily ingestion did not complete its bounded scope")
    if not options.skip_freshness and not options.calendar_only:
        result = await freshness(
            database or Database(Settings.from_env().database_url),
            min_symbols=number("ALPHADECK_MARKET_FRESHNESS_MIN_SYMBOLS", 250),
        )
        if not result["ok"]:
            raise ValueError(
                "Market freshness gate failed: " + json.dumps(result, ensure_ascii=False)
            )
    return dict(dryRun=False, **plan, calendar=calendar_result, ingestion=ingestion)


async def main(argv=None):
    load_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("maintain", "freshness"))
    parser.add_argument("--calendar-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-freshness", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-lag-sessions", type=int, default=0)
    parser.add_argument(
        "--min-symbols",
        type=int,
        default=int(os.getenv("ALPHADECK_MARKET_FRESHNESS_MIN_SYMBOLS", "1")),
    )
    args = parser.parse_args(argv)
    if args.min_symbols < 1 or args.max_lag_sessions < 0:
        parser.error("symbol count must be positive and session lag non-negative")
    result = (
        await maintain(args)
        if args.command == "maintain"
        else await freshness(
            Database(Settings.from_env().database_url),
            max_lag_sessions=args.max_lag_sessions,
            min_symbols=args.min_symbols,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
