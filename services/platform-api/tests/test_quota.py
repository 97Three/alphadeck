import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from alphadeck_platform.database import Database
from alphadeck_platform.quota import Quota, QuotaError, window


def limited(database_url, actor, limit):
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """INSERT INTO user_quota_overrides
            (id,user_id,metric,"limit",enforcement,window_type,updated_at)
            VALUES (%s,%s,'projects.owned',%s,'hard','lifetime',now())""",
            (str(uuid4()), actor["user_id"], limit),
        )
    return Quota(Database(database_url))


def test_quota_concurrency_and_settlement_replay(database_url, actor):
    quota = limited(database_url, actor, 2)

    async def run():
        results = await asyncio.gather(
            *[quota.reserve(actor["user_id"], "projects.owned", 1, str(uuid4())) for _ in range(5)],
            return_exceptions=True,
        )
        accepted = [result for result in results if isinstance(result, dict)]
        denied = [result for result in results if isinstance(result, QuotaError)]
        assert len(accepted) == 2 and len(denied) == 3
        assert all(error.code == "QUOTA_EXCEEDED" for error in denied)
        identity = accepted[0]["reservation"]["id"]
        results = await asyncio.gather(
            quota.settle(identity, 1, "test"), quota.settle(identity, 1, "test")
        )
        assert sum(result["idempotent"] for result in results) == 1
        assert results[0]["eventId"] == results[1]["eventId"]
        await quota.release(accepted[1]["reservation"]["id"])
        with psycopg.connect(database_url) as connection:
            used, reserved = connection.execute(
                "SELECT used,reserved FROM usage_buckets WHERE actor_user_id=%s AND metric='projects.owned'",
                (actor["user_id"],),
            ).fetchone()
            assert (used, reserved) == (1, 0)

    asyncio.run(run())


def test_quota_keys_and_hard_settlement_are_checked(database_url, actor):
    quota = limited(database_url, actor, 2)

    async def run():
        key = str(uuid4())
        first = await quota.reserve(actor["user_id"], "projects.owned", 1, key)
        assert (await quota.reserve(actor["user_id"], "projects.owned", 1, key))["reservation"][
            "idempotent"
        ]
        with pytest.raises(QuotaError) as conflict:
            await quota.reserve(actor["user_id"], "projects.owned", 2, key)
        assert conflict.value.code == "QUOTA_IDEMPOTENCY_CONFLICT"
        with pytest.raises(QuotaError) as excess:
            await quota.settle(first["reservation"]["id"], 2, "test")
        assert excess.value.code == "QUOTA_SETTLEMENT_EXCEEDS_RESERVATION"
        released = await quota.release(first["reservation"]["id"])
        assert released["reserved"] == 0
        assert (await quota.release(first["reservation"]["id"]))["idempotent"]
        with pytest.raises(QuotaError):
            await quota.reserve(actor["user_id"], "projects.owned", 1, key)

    asyncio.run(run())


def test_expiration_accounting_commits_when_retry_is_rejected(database_url, actor):
    quota = limited(database_url, actor, 1)

    async def run():
        start = datetime.now(UTC)
        key = str(uuid4())
        await quota.reserve(actor["user_id"], "projects.owned", 1, key, at=start, ttl=1)
        with pytest.raises(QuotaError) as expired:
            await quota.reserve(
                actor["user_id"], "projects.owned", 1, key, at=start + timedelta(seconds=2)
            )
        assert expired.value.code == "QUOTA_RESERVATION_EXPIRED"
        with psycopg.connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM quota_reservations WHERE idempotency_key=%s", (key,)
                ).fetchone()[0]
                == "expired"
            )
            assert (
                connection.execute(
                    "SELECT reserved FROM usage_buckets WHERE actor_user_id=%s", (actor["user_id"],)
                ).fetchone()[0]
                == 0
            )
        assert (
            await quota.reserve(
                actor["user_id"], "projects.owned", 1, str(uuid4()), at=start + timedelta(seconds=2)
            )
        )["allowed"]

    asyncio.run(run())


def test_usage_replay_and_negative_adjustment_are_atomic(database_url, actor):
    quota = Quota(Database(database_url))

    async def run():
        key = str(uuid4())
        with pytest.raises(QuotaError) as underflow:
            await quota.record_usage(actor["user_id"], "test.usage", -1, str(uuid4()), "test")
        assert underflow.value.code == "QUOTA_USAGE_UNDERFLOW"
        first = await quota.record_usage(actor["user_id"], "test.usage", 2, key, "test")
        replay = await quota.record_usage(actor["user_id"], "test.usage", 2, key, "test")
        assert replay["idempotent"] and replay["eventId"] == first["eventId"]
        with pytest.raises(QuotaError):
            await quota.record_usage(actor["user_id"], "test.usage", 3, key, "test")
        adjusted = await quota.record_usage(
            actor["user_id"], "test.usage", -1, str(uuid4()), "adjustment"
        )
        assert adjusted["counter"]["used"] == 1

    asyncio.run(run())


def test_reservation_renewal_and_cleanup(database_url, actor):
    quota = limited(database_url, actor, 1)

    async def run():
        start = datetime.now(UTC)
        reservation = await quota.reserve(
            actor["user_id"], "projects.owned", 1, str(uuid4()), at=start, ttl=1
        )
        renewed = await quota.renew(reservation["reservation"]["id"], at=start, ttl=2)
        assert renewed["expiresAt"] > reservation["reservation"]["expiresAt"]
        result = await quota.cleanup_expired(at=start + timedelta(seconds=3))
        assert result["expired"] >= 1
        with pytest.raises(QuotaError):
            await quota.renew(reservation["reservation"]["id"], at=start + timedelta(seconds=3))

    asyncio.run(run())


def test_quota_calendar_windows_use_utc():
    at = datetime(2026, 12, 31, 23, 59, 58, tzinfo=UTC)
    assert window("month", at)["end"] == datetime(2027, 1, 1, tzinfo=UTC)
    assert window("day", at)["start"] == datetime(2026, 12, 31, tzinfo=UTC)
    assert window("fixed", at, 60)["start"].second == 0
