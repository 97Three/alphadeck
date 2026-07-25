import json
import math
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from .config import ROOT
from .sessions import iso

DEFAULTS = json.loads((ROOT / "config/quota-defaults.json").read_text())
WINDOWS = {"minute", "hour", "day", "month", "fixed", "lifetime"}


class QuotaError(Exception):
    def __init__(self, code, message, status=500, decision=None):
        self.code, self.message, self.status, self.decision = code, message, status, decision
        super().__init__(message)


def conflict(message="The idempotency key was already used for a different quota operation."):
    return QuotaError("QUOTA_IDEMPOTENCY_CONFLICT", message, 409)


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def quantity(value, *, zero=False, signed=False):
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+" if signed else r"[0-9]+", value):
        result = int(value)
    elif type(value) is int:
        result = value
    elif (
        type(value) is float
        and math.isfinite(value)
        and value.is_integer()
        and abs(value) <= 9007199254740991
    ):
        result = int(value)
    else:
        raise TypeError("Quota quantity must be an integer")
    if not signed and result < (0 if zero else 1):
        raise ValueError("Invalid quota quantity")
    return result


def identifier(value, maximum):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError("Invalid quota identifier")


def metric_name(metric):
    if not isinstance(metric, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,159}", metric):
        raise ValueError("Invalid quota metric")


def window(kind, at, seconds=None):
    at = utc(at)
    if kind not in WINDOWS:
        raise QuotaError("INVALID_QUOTA_POLICY", "Unsupported quota window type")
    if kind == "lifetime":
        return {
            "start": datetime(1970, 1, 1, tzinfo=UTC),
            "end": datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=UTC),
        }
    if kind == "fixed":
        if type(seconds) is not int or seconds <= 0:
            raise ValueError("Fixed quota window requires positive window seconds")
        start = datetime.fromtimestamp(math.floor(at.timestamp() / seconds) * seconds, UTC)
        return {"start": start, "end": start + timedelta(seconds=seconds)}
    if kind == "minute":
        start = at.replace(second=0, microsecond=0)
        return {"start": start, "end": start + timedelta(minutes=1)}
    if kind == "hour":
        start = at.replace(minute=0, second=0, microsecond=0)
        return {"start": start, "end": start + timedelta(hours=1)}
    start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "day":
        return {"start": start, "end": start + timedelta(days=1)}
    start = start.replace(day=1)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return {"start": start, "end": end}


def counter(policy, used, reserved, requested=0):
    limit = None if policy["unlimited"] else policy["limit"]
    projected = used + reserved + requested
    return {
        "used": used,
        "reserved": reserved,
        "requested": requested,
        "limit": limit,
        "remaining": None if limit is None else max(0, limit - projected),
        "exceeded": limit is not None and projected > limit,
    }


async def one(tx, query, args=()):
    return await (await tx.execute(query, args)).fetchone()


async def advisory(tx, namespace, key):
    await tx.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (namespace + ":" + key,)
    )


async def bucket_lock(tx, actor, metric, selected):
    key = ":".join((actor, metric, iso(selected["start"]), iso(selected["end"])))
    await advisory(tx, "quota-bucket", key)


async def resolve_policy(tx, actor, metric, at):
    user = await one(tx, "SELECT role,quota_profile_id FROM auth_users WHERE id=%s", (actor,))
    if not user:
        raise QuotaError("QUOTA_ACTOR_NOT_FOUND", "Quota actor does not exist.", 409)
    override = await one(
        tx,
        "SELECT * FROM user_quota_overrides WHERE user_id=%s AND metric=%s AND (expires_at IS NULL OR expires_at>%s)",
        (actor, metric, at),
    )
    rule = await one(
        tx,
        "SELECT * FROM quota_rules WHERE profile_id=%s AND metric=%s",
        (user["quota_profile_id"], metric),
    )
    if not rule:
        rule = await one(
            tx,
            "SELECT r.* FROM quota_rules r JOIN quota_profiles p ON p.id=r.profile_id WHERE p.is_default=true AND r.metric=%s ORDER BY p.id LIMIT 1",
            (metric,),
        )
    builtin = next((item for item in DEFAULTS["rules"] if item["metric"] == metric), None)
    if builtin:
        builtin = {
            "limit": int(builtin["limit"]),
            "enforcement": builtin["enforcement"],
            "window_type": builtin["windowType"],
            "window_seconds": builtin["windowSeconds"],
            "reservation_ttl_seconds": builtin["reservationTtlSeconds"],
        }
    base = (
        override
        or rule
        or builtin
        or {"window_type": "month", "window_seconds": None, "reservation_ttl_seconds": 900}
    )
    if base["window_type"] not in WINDOWS:
        raise QuotaError("INVALID_QUOTA_POLICY", "Unsupported quota window type")
    result = {
        "actorUserId": actor,
        "metric": metric,
        "windowType": base["window_type"],
        "windowSeconds": base["window_seconds"],
        "reservationTtlSeconds": base["reservation_ttl_seconds"],
    }
    if user["role"] == "admin":
        return {
            **result,
            "source": "administrator",
            "unlimited": True,
            "limit": None,
            "enforcement": "observe",
            "enforcementExempt": True,
        }
    if override and not override["is_unlimited"] and override["limit"] is None:
        raise QuotaError("INVALID_QUOTA_POLICY", "A metered quota override has no limit.")
    source = (
        "user-override"
        if override
        else "profile"
        if rule
        else "builtin-default"
        if builtin
        else "unconfigured"
    )
    unlimited = override["is_unlimited"] if override else not (rule or builtin)
    enforcement = base.get("enforcement", "observe")
    return {
        **result,
        "source": source,
        "unlimited": bool(unlimited),
        "limit": None if unlimited else base["limit"],
        "enforcement": enforcement if enforcement in {"hard", "warn"} else "observe",
        "enforcementExempt": bool(unlimited),
    }


async def bucket(tx, actor, metric, selected):
    return await one(
        tx,
        """INSERT INTO usage_buckets (id,actor_user_id,metric,window_start,window_end,updated_at)
        VALUES (%s,%s,%s,%s,%s,now()) ON CONFLICT (actor_user_id,metric,window_start,window_end)
        DO UPDATE SET metric=excluded.metric RETURNING *""",
        (str(uuid4()), actor, metric, selected["start"], selected["end"]),
    )


async def structural_capacity(tx, actor, metric, current, requested=1, *, at=None):
    at = at or datetime.now(UTC)
    policy = await resolve_policy(tx, actor, metric, at)
    counts = counter(policy, 0, quantity(current, zero=True), quantity(requested, zero=True))
    if policy["enforcement"] == "hard" and counts["exceeded"]:
        raise QuotaError(
            "QUOTA_EXCEEDED",
            "Quota capacity exceeded.",
            429,
            {
                "policy": policy,
                "counter": counts,
                "window": window(policy["windowType"], at, policy["windowSeconds"]),
            },
        )
    return policy


async def expire(tx, bucket_id, at):
    await tx.execute(
        """WITH expired AS (
        UPDATE quota_reservations SET status='expired',released_at=%s,updated_at=%s
        WHERE bucket_id=%s AND status='active' AND expires_at<=%s RETURNING reserved_quantity
        ), released AS (SELECT COALESCE(sum(reserved_quantity),0)::bigint AS quantity FROM expired)
        UPDATE usage_buckets SET reserved=GREATEST(0::bigint,reserved-released.quantity),version=version+1,updated_at=%s
        FROM released WHERE id=%s AND released.quantity>0""",
        (at, at, bucket_id, at, at, bucket_id),
    )
    return await one(tx, "SELECT * FROM usage_buckets WHERE id=%s", (bucket_id,))


def reservation_view(row, current, idempotent):
    limit = row["policy_limit"]
    return {
        "id": row["id"],
        "idempotencyKey": row["idempotency_key"],
        "actorUserId": row["actor_user_id"],
        "projectId": row["project_id"],
        "metric": row["metric"],
        "status": row["status"],
        "committedQuantity": row["committed_quantity"],
        "enforcement": row["policy_enforcement"],
        "enforcementExempt": row["enforcement_exempt"],
        "used": current["used"],
        "reserved": current["reserved"],
        "requested": row["reserved_quantity"],
        "limit": limit,
        "remaining": None
        if limit is None
        else max(0, limit - current["used"] - current["reserved"]),
        "exceeded": limit is not None and current["used"] + current["reserved"] > limit,
        "windowStart": utc(current["window_start"]),
        "windowEnd": utc(current["window_end"]),
        "expiresAt": utc(row["expires_at"]),
        "idempotent": idempotent,
    }


async def locked_reservation(tx, identity=None, key=None):
    if not identity and not key:
        raise ValueError("Reservation identity is required")
    row = await one(
        tx,
        "SELECT * FROM quota_reservations WHERE id=%s"
        if identity
        else "SELECT * FROM quota_reservations WHERE idempotency_key=%s",
        (identity or key,),
    )
    if not row:
        raise QuotaError("QUOTA_RESERVATION_NOT_FOUND", "Quota reservation was not found.", 409)
    current = await one(tx, "SELECT * FROM usage_buckets WHERE id=%s", (row["bucket_id"],))
    await bucket_lock(
        tx,
        row["actor_user_id"],
        row["metric"],
        {"start": current["window_start"], "end": current["window_end"]},
    )
    return await one(tx, "SELECT * FROM quota_reservations WHERE id=%s FOR UPDATE", (row["id"],))


def usage_result(event, current, limit, idempotent):
    return {
        "eventId": event["id"],
        "reservationId": event["reservation_id"],
        "actorUserId": event["actor_user_id"],
        "projectId": event["project_id"],
        "metric": event["metric"],
        "quantity": event["quantity"],
        "enforcementExempt": event["enforcement_exempt"],
        "sourceType": event["source_type"],
        "sourceId": event["source_id"],
        "occurredAt": utc(event["occurred_at"]),
        "idempotent": idempotent,
        "counter": {
            "used": current["used"],
            "reserved": current["reserved"],
            "limit": limit,
            "remaining": None
            if limit is None
            else max(0, limit - current["used"] - current["reserved"]),
            "exceeded": limit is not None and current["used"] + current["reserved"] > limit,
        },
    }


class Quota:
    def __init__(self, database):
        self.database = database

    async def _run(self, operation, **arguments):
        async with self.database.transaction() as tx:
            result = await operation(tx, **arguments)
        # Expiration accounting commits even when the caller receives an expired-operation error.
        if isinstance(result, QuotaError):
            raise result
        return result

    async def policy(self, actor, metric, at=None):
        metric_name(metric)
        return await self._run(
            resolve_policy, actor=actor, metric=metric, at=at or datetime.now(UTC)
        )

    async def reserve(self, actor, metric, requested, key, *, project_id=None, at=None, ttl=None):
        identifier(actor, 191)
        identifier(key, 240)
        metric_name(metric)
        result = await self._run(
            self._reserve,
            actor=actor,
            metric=metric,
            requested=quantity(requested),
            key=key,
            project_id=project_id or None,
            at=at or datetime.now(UTC),
            ttl=ttl,
        )
        if not result["allowed"]:
            raise QuotaError(
                "QUOTA_EXCEEDED",
                "The requested operation exceeds the available quota.",
                429,
                result,
            )
        return result

    async def _reserve(self, tx, *, actor, metric, requested, key, project_id, at, ttl):
        await advisory(tx, "quota-reserve", key)
        existing = await one(
            tx, "SELECT * FROM quota_reservations WHERE idempotency_key=%s", (key,)
        )
        if existing:
            if (
                existing["actor_user_id"],
                existing["metric"],
                existing["reserved_quantity"],
                existing["project_id"],
            ) != (actor, metric, requested, project_id):
                raise conflict()
            current = await one(
                tx, "SELECT * FROM usage_buckets WHERE id=%s", (existing["bucket_id"],)
            )
            if existing["status"] == "active" and utc(existing["expires_at"]) <= utc(at):
                await bucket_lock(
                    tx,
                    actor,
                    metric,
                    {"start": current["window_start"], "end": current["window_end"]},
                )
                await expire(tx, current["id"], at)
                return QuotaError(
                    "QUOTA_RESERVATION_EXPIRED",
                    "The idempotent quota reservation expired; use a new operation key.",
                    409,
                )
            if existing["status"] in {"released", "expired"}:
                raise conflict("The idempotency key belongs to a finalized quota reservation.")
            policy = {
                "actorUserId": actor,
                "metric": metric,
                "source": "administrator" if existing["enforcement_exempt"] else "profile",
                "unlimited": existing["policy_limit"] is None,
                "limit": existing["policy_limit"],
                "enforcement": existing["policy_enforcement"],
                "windowType": existing["policy_window_type"],
                "windowSeconds": None,
                "reservationTtlSeconds": max(
                    1, math.ceil((existing["expires_at"] - existing["created_at"]).total_seconds())
                ),
                "enforcementExempt": existing["enforcement_exempt"],
            }
            view = reservation_view(existing, current, True)
        else:
            policy = await resolve_policy(tx, actor, metric, at)
            selected = window(policy["windowType"], at, policy["windowSeconds"])
            await bucket_lock(tx, actor, metric, selected)
            current = await bucket(tx, actor, metric, selected)
            current = await expire(tx, current["id"], at)
            counters = counter(policy, current["used"], current["reserved"], requested)
            if counters["exceeded"] and policy["enforcement"] == "hard":
                return {
                    "allowed": False,
                    "mode": "metered",
                    "policy": policy,
                    "counter": counters,
                    "window": selected,
                    "reservation": None,
                }
            lifetime = (
                policy["reservationTtlSeconds"]
                if ttl is None
                else min(policy["reservationTtlSeconds"], ttl)
            )
            if type(lifetime) is not int or lifetime <= 0:
                raise ValueError("Reservation TTL must be positive")
            current = await one(
                tx,
                "UPDATE usage_buckets SET reserved=reserved+%s,version=version+1,updated_at=%s WHERE id=%s RETURNING *",
                (requested, at, current["id"]),
            )
            row = await one(
                tx,
                """INSERT INTO quota_reservations
                (id,idempotency_key,actor_user_id,project_id,bucket_id,metric,reserved_quantity,policy_limit,
                 policy_enforcement,policy_window_type,enforcement_exempt,expires_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    str(uuid4()),
                    key,
                    actor,
                    project_id,
                    current["id"],
                    metric,
                    requested,
                    policy["limit"],
                    policy["enforcement"],
                    policy["windowType"],
                    policy["enforcementExempt"],
                    at + timedelta(seconds=lifetime),
                    at,
                ),
            )
            view = reservation_view(row, current, False)
        return {
            "allowed": True,
            "mode": "unlimited" if policy["unlimited"] else "metered",
            "policy": policy,
            "counter": {
                key: view[key]
                for key in ("used", "reserved", "requested", "limit", "remaining", "exceeded")
            },
            "window": {"start": view["windowStart"], "end": view["windowEnd"]},
            "reservation": view,
        }

    async def settle(
        self,
        identity,
        actual,
        source_type,
        *,
        source_id=None,
        event_key=None,
        at=None,
        metadata=None,
    ):
        identifier(source_type, 100)
        return await self._run(
            self._settle,
            identity=identity,
            actual=quantity(actual, zero=True),
            source_type=source_type,
            source_id=source_id,
            event_key=event_key,
            at=at or datetime.now(UTC),
            metadata=metadata or {},
        )

    async def _settle(
        self, tx, *, identity, actual, source_type, source_id, event_key, at, metadata
    ):
        event_key = event_key or "quota:settlement:" + identity
        identifier(event_key, 240)
        await advisory(tx, "quota-event", event_key)
        reservation = await locked_reservation(tx, identity)
        existing = await one(tx, "SELECT * FROM usage_events WHERE reservation_id=%s", (identity,))
        current = await one(
            tx, "SELECT * FROM usage_buckets WHERE id=%s", (reservation["bucket_id"],)
        )
        if existing:
            if (existing["quantity"], existing["source_type"], existing["source_id"]) != (
                actual,
                source_type,
                source_id,
            ):
                raise conflict("The reservation was already settled with different usage.")
            return usage_result(existing, current, reservation["policy_limit"], True)
        if reservation["status"] == "active" and utc(reservation["expires_at"]) <= utc(at):
            await expire(tx, current["id"], at)
            return QuotaError(
                "QUOTA_RESERVATION_EXPIRED",
                "Quota reservation expired before it could be settled.",
                409,
            )
        if reservation["status"] != "active":
            raise QuotaError("QUOTA_RESERVATION_INACTIVE", "Quota reservation is not active.", 409)
        if (
            reservation["policy_enforcement"] == "hard"
            and not reservation["enforcement_exempt"]
            and actual > reservation["reserved_quantity"]
        ):
            raise QuotaError(
                "QUOTA_SETTLEMENT_EXCEEDS_RESERVATION",
                "A hard quota settlement cannot commit more than the reserved quantity.",
                409,
            )
        if await one(tx, "SELECT id FROM usage_events WHERE idempotency_key=%s", (event_key,)):
            raise conflict()
        current = await one(
            tx,
            """UPDATE usage_buckets SET reserved=GREATEST(0::bigint,reserved-%s),
            used=used+%s,version=version+1,updated_at=%s WHERE id=%s RETURNING *""",
            (reservation["reserved_quantity"], actual, at, current["id"]),
        )
        await tx.execute(
            "UPDATE quota_reservations SET status='settled',committed_quantity=%s,settled_at=%s,updated_at=%s WHERE id=%s",
            (actual, at, at, identity),
        )
        event = await one(
            tx,
            """INSERT INTO usage_events (id,idempotency_key,actor_user_id,project_id,reservation_id,
            bucket_id,metric,quantity,source_type,source_id,enforcement_exempt,occurred_at,metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                str(uuid4()),
                event_key,
                reservation["actor_user_id"],
                reservation["project_id"],
                identity,
                reservation["bucket_id"],
                reservation["metric"],
                actual,
                source_type,
                source_id,
                reservation["enforcement_exempt"],
                at,
                Jsonb(metadata),
            ),
        )
        return usage_result(event, current, reservation["policy_limit"], False)

    async def release(self, identity, at=None):
        return await self._run(self._release, identity=identity, at=at or datetime.now(UTC))

    async def _release(self, tx, identity, at):
        row = await locked_reservation(tx, identity)
        previous_status = row["status"]
        if row["status"] == "active":
            await tx.execute(
                "UPDATE usage_buckets SET reserved=GREATEST(0::bigint,reserved-%s),version=version+1,updated_at=%s WHERE id=%s",
                (row["reserved_quantity"], at, row["bucket_id"]),
            )
            row = await one(
                tx,
                "UPDATE quota_reservations SET status='released',released_at=%s,updated_at=%s WHERE id=%s RETURNING *",
                (at, at, identity),
            )
        current = await one(tx, "SELECT * FROM usage_buckets WHERE id=%s", (row["bucket_id"],))
        return reservation_view(row, current, previous_status != "active")

    async def renew(self, identity, *, ttl=None, at=None):
        return await self._run(self._renew, identity=identity, ttl=ttl, at=at or datetime.now(UTC))

    async def _renew(self, tx, identity, ttl, at):
        row = await locked_reservation(tx, identity)
        if row["status"] == "active" and utc(row["expires_at"]) <= utc(at):
            await expire(tx, row["bucket_id"], at)
            return QuotaError(
                "QUOTA_RESERVATION_INACTIVE",
                "Only an unexpired active reservation can be renewed.",
                409,
            )
        if row["status"] != "active":
            raise QuotaError(
                "QUOTA_RESERVATION_INACTIVE",
                "Only an unexpired active reservation can be renewed.",
                409,
            )
        policy = await resolve_policy(tx, row["actor_user_id"], row["metric"], at)
        ttl = policy["reservationTtlSeconds"] if ttl is None else ttl
        if type(ttl) is not int or ttl <= 0:
            raise ValueError("Reservation TTL must be positive")
        ttl = min(ttl, policy["reservationTtlSeconds"])
        row = await one(
            tx,
            "UPDATE quota_reservations SET expires_at=%s,updated_at=%s WHERE id=%s RETURNING *",
            (at + timedelta(seconds=ttl), at, identity),
        )
        current = await one(tx, "SELECT * FROM usage_buckets WHERE id=%s", (row["bucket_id"],))
        return reservation_view(row, current, False)

    async def record_usage(
        self,
        actor,
        metric,
        actual,
        key,
        source_type,
        *,
        source_id=None,
        project_id=None,
        at=None,
        metadata=None,
    ):
        identifier(actor, 191)
        identifier(key, 240)
        identifier(source_type, 100)
        metric_name(metric)
        return await self._run(
            self._record,
            actor=actor,
            metric=metric,
            actual=quantity(actual, signed=True),
            key=key,
            source_type=source_type,
            source_id=source_id,
            project_id=project_id or None,
            at=at or datetime.now(UTC),
            metadata=metadata or {},
        )

    async def _record(
        self, tx, *, actor, metric, actual, key, source_type, source_id, project_id, at, metadata
    ):
        await advisory(tx, "quota-event", key)
        existing = await one(tx, "SELECT * FROM usage_events WHERE idempotency_key=%s", (key,))
        if existing:
            if (
                existing["actor_user_id"],
                existing["metric"],
                existing["quantity"],
                existing["project_id"],
                existing["source_type"],
                existing["source_id"],
            ) != (actor, metric, actual, project_id, source_type, source_id):
                raise conflict()
            current = await one(
                tx, "SELECT * FROM usage_buckets WHERE id=%s", (existing["bucket_id"],)
            )
            if current is None:
                current = await one(
                    tx,
                    "SELECT * FROM usage_buckets WHERE actor_user_id=%s AND metric=%s AND window_start<=%s AND window_end>%s ORDER BY window_start DESC LIMIT 1",
                    (actor, metric, existing["occurred_at"], existing["occurred_at"]),
                )
            policy = await resolve_policy(tx, actor, metric, existing["occurred_at"])
            return usage_result(existing, current, policy["limit"], True)
        policy = await resolve_policy(tx, actor, metric, at)
        selected = window(policy["windowType"], at, policy["windowSeconds"])
        await bucket_lock(tx, actor, metric, selected)
        current = await bucket(tx, actor, metric, selected)
        updated = await one(
            tx,
            "UPDATE usage_buckets SET used=used+%s,version=version+1,updated_at=%s WHERE id=%s AND used+%s>=0 RETURNING *",
            (actual, at, current["id"], actual),
        )
        if not updated:
            raise QuotaError(
                "QUOTA_USAGE_UNDERFLOW",
                "A quota usage adjustment cannot reduce the bucket below zero.",
                409,
            )
        event = await one(
            tx,
            """INSERT INTO usage_events (id,idempotency_key,actor_user_id,project_id,bucket_id,metric,
            quantity,source_type,source_id,enforcement_exempt,occurred_at,metadata)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                str(uuid4()),
                key,
                actor,
                project_id,
                current["id"],
                metric,
                actual,
                source_type,
                source_id,
                policy["enforcementExempt"],
                at,
                Jsonb(metadata),
            ),
        )
        return usage_result(event, updated, policy["limit"], False)

    async def consume(self, actor, metric, actual, key, source_type, **options):
        reservation = await self.reserve(
            actor, metric, actual, key, project_id=options.get("project_id")
        )
        return await self.settle(
            reservation["reservation"]["id"],
            actual,
            source_type,
            source_id=options.get("source_id"),
            event_key=options.get("event_key"),
            at=options.get("at"),
            metadata=options.get("metadata"),
        )

    async def cleanup_expired(self, *, at=None, batch_size=200):
        at = at or datetime.now(UTC)
        async with self.database.transaction() as tx:
            leader = await one(
                tx,
                "SELECT pg_try_advisory_xact_lock(hashtextextended('quota-maintenance:expired-reservations',0)) AS acquired",
            )
            if not leader["acquired"]:
                return {"scanned": 0, "expired": 0}
            candidates = await (
                await tx.execute(
                    "SELECT id FROM quota_reservations WHERE status='active' AND expires_at<=%s ORDER BY expires_at LIMIT %s",
                    (at, max(1, min(1000, batch_size))),
                )
            ).fetchall()
            expired = 0
            for candidate in candidates:
                row = await locked_reservation(tx, candidate["id"])
                if row["status"] != "active" or utc(row["expires_at"]) > utc(at):
                    continue
                await tx.execute(
                    "UPDATE usage_buckets SET reserved=GREATEST(0::bigint,reserved-%s),version=version+1,updated_at=%s WHERE id=%s",
                    (row["reserved_quantity"], at, row["bucket_id"]),
                )
                await tx.execute(
                    "UPDATE quota_reservations SET status='expired',released_at=%s,updated_at=%s WHERE id=%s",
                    (at, at, row["id"]),
                )
                expired += 1
            return {"scanned": len(candidates), "expired": expired}
