from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from .messages import js_slice
from .quota import advisory, one
from .runtime_store import insert, snake, update

PROVIDER = "evolvable-memory-http-v1"
CONTROL_POLICY = "alphadeck-personalization-v1"


def control_state(row):
    return {
        "configured": bool(row),
        "personalizationEnabled": row["personalization_enabled"] if row else False,
        "policyVersion": row["policy_version"] if row else CONTROL_POLICY,
        **{
            key: row[column] if row else None
            for key, column in (
                ("enabledAt", "enabled_at"),
                ("disabledAt", "disabled_at"),
                ("updatedAt", "updated_at"),
            )
        },
    }


def memory_use(row):
    if not row:
        return None
    result = {
        key: row[snake(key)]
        for key in (
            "provider",
            "projectId",
            "requestId",
            "tenantId",
            "integrationScopeSha256",
            "subjectId",
            "policyVersion",
            "validAt",
            "knownAt",
            "sourceProjectionSha256",
            "deliveredContextSha256",
            "exposedRevisionIds",
            "status",
            "exposedAt",
        )
    }
    result.update(
        {
            key: str(row[snake(key)]) if row[snake(key)] is not None else None
            for key in ("traceId", "policyId", "providerUsageId")
        }
    )
    value = result["exposedRevisionIds"]
    result["exposedRevisionIds"] = (
        value if isinstance(value, list) and all(isinstance(item, str) for item in value) else []
    )
    result["status"] = "legacy_empty" if result["status"] == "legacy_empty" else "exposed"
    return result


class MemoryRepository:
    def __init__(self, database):
        self.database = database

    async def control(self, subject):
        return control_state(
            await self.database.fetch_one(
                "SELECT * FROM personal_memory_controls WHERE subject_id=%s", (subject,)
            )
        )

    async def set_control(self, subject, enabled):
        async with self.database.transaction() as tx:
            await advisory(tx, "personal-memory-control", subject)
            row = await one(
                tx,
                "SELECT * FROM personal_memory_controls WHERE subject_id=%s FOR UPDATE",
                (subject,),
            )
            if row and row["personalization_enabled"] == enabled:
                return {**control_state(row), "changed": False}
            now = datetime.now(UTC)
            values = {
                "personalization_enabled": enabled,
                "policy_version": CONTROL_POLICY,
                "enabled_at": now if enabled else None,
                "disabled_at": None if enabled else now,
            }
            row = (
                await update(tx, "personal_memory_controls", row["id"], values)
                if row
                else await insert(
                    tx,
                    "personal_memory_controls",
                    {"id": str(uuid4()), "subject_id": subject, **values, "updated_at": now},
                )
            )
            return {**control_state(row), "changed": True}

    async def save_use(self, input):
        async with self.database.transaction() as tx:
            await advisory(
                tx,
                "external-memory-use",
                input["provider"] + ":" + input["projectId"] + ":" + input["requestId"],
            )
            row = await one(
                tx,
                "SELECT * FROM external_memory_uses WHERE provider=%s AND project_id=%s AND request_id=%s FOR UPDATE",
                (input["provider"], input["projectId"], input["requestId"]),
            )
            if not row:
                fields = {
                    snake(key): input[key]
                    for key in (
                        "provider",
                        "projectId",
                        "requestId",
                        "tenantId",
                        "integrationScopeSha256",
                        "subjectId",
                        "traceId",
                        "providerUsageId",
                        "policyId",
                        "policyVersion",
                        "validAt",
                        "knownAt",
                        "sourceProjectionSha256",
                        "deliveredContextSha256",
                    )
                }
                row = await insert(
                    tx,
                    "external_memory_uses",
                    {
                        "id": str(uuid4()),
                        **fields,
                        "exposed_revision_ids": Jsonb(input["exposedRevisionIds"]),
                        "status": input.get("status", "exposed"),
                        "exposed_at": input.get("exposedAt") or datetime.now(UTC),
                        "updated_at": datetime.now(UTC),
                    },
                )
            stored = memory_use(row)
            if any(
                stored[key] != input[key]
                for key in (
                    "tenantId",
                    "integrationScopeSha256",
                    "subjectId",
                    "traceId",
                    "providerUsageId",
                    "policyId",
                    "policyVersion",
                    "sourceProjectionSha256",
                    "deliveredContextSha256",
                    "exposedRevisionIds",
                )
            ):
                raise ValueError("External memory use idempotency collision.")
            return stored

    async def find_use(self, project, request):
        return memory_use(
            await self.database.fetch_one(
                "SELECT * FROM external_memory_uses WHERE provider=%s AND project_id=%s AND request_id=%s",
                (PROVIDER, project, request),
            )
        )

    async def summary(self, subject):
        uses = await self.database.fetch_one(
            """SELECT count(*) FILTER (WHERE status='exposed') AS exposed,
            coalesce(sum(jsonb_array_length(CASE WHEN jsonb_typeof(exposed_revision_ids)='array' THEN exposed_revision_ids ELSE '[]'::jsonb END)) FILTER (WHERE status='exposed'),0) AS revisions,
            count(*) FILTER (WHERE status<>'exposed') AS legacy,max(exposed_at) FILTER (WHERE status='exposed') AS latest
            FROM external_memory_uses WHERE provider=%s AND subject_id=%s""",
            (PROVIDER, subject),
        )
        feedback = await self.database.fetch_one(
            """SELECT count(*) FILTER (WHERE status='completed') AS completed,
            count(*) FILTER (WHERE status='completed' AND kind='helpful') AS helpful,
            count(*) FILTER (WHERE status='completed' AND kind='rejected') AS rejected,
            count(*) FILTER (WHERE status='pending') AS pending,count(*) FILTER (WHERE status='failed') AS failed
            FROM personal_memory_feedback_receipts WHERE provider=%s AND subject_id=%s""",
            (PROVIDER, subject),
        )
        return {
            "exposedRunCount": uses["exposed"],
            "exposedRevisionReferenceCount": uses["revisions"],
            "legacyEmptyAttributionCount": uses["legacy"],
            "lastExposedAt": uses["latest"],
            "completedFeedbackCount": feedback["completed"],
            "helpfulFeedbackCount": feedback["helpful"],
            "rejectedFeedbackCount": feedback["rejected"],
            "pendingFeedbackCount": feedback["pending"],
            "failedFeedbackCount": feedback["failed"],
        }

    async def begin_feedback(self, input):
        async with self.database.transaction() as tx:
            await advisory(
                tx,
                "personal-memory-feedback",
                input["projectId"] + ":" + input["requestId"] + ":" + input["revisionId"],
            )
            row = await one(
                tx,
                "SELECT * FROM personal_memory_feedback_receipts WHERE provider=%s AND project_id=%s AND request_id=%s AND revision_id=%s FOR UPDATE",
                (PROVIDER, input["projectId"], input["requestId"], input["revisionId"]),
            )
            if row and any(
                row[snake(key)] != input[key] for key in ("subjectId", "eventId", "kind")
            ):
                raise ValueError(
                    "Feedback for this exposed revision has already been recorded with different semantics."
                )
            if row and row["status"] == "completed":
                return {"receipt": row, "shouldSubmit": False}
            if row:
                if row["status"] == "failed":
                    row = await update(
                        tx,
                        "personal_memory_feedback_receipts",
                        row["id"],
                        {"status": "pending", "last_error_code": None},
                    )
            else:
                row = await insert(
                    tx,
                    "personal_memory_feedback_receipts",
                    {
                        "id": str(uuid4()),
                        "provider": PROVIDER,
                        **{
                            snake(key): input[key]
                            for key in (
                                "projectId",
                                "requestId",
                                "subjectId",
                                "revisionId",
                                "eventId",
                                "kind",
                            )
                        },
                        "updated_at": datetime.now(UTC),
                    },
                )
            return {"receipt": row, "shouldSubmit": True}

    async def complete_feedback(self, identity, outcome):
        async with self.database.transaction() as tx:
            row = await one(
                tx,
                "SELECT * FROM personal_memory_feedback_receipts WHERE id=%s FOR UPDATE",
                (identity,),
            )
            if not row:
                raise ValueError("Memory feedback receipt does not exist")
            if row["status"] == "completed" and str(row["outcome_id"]) != outcome:
                raise ValueError("Memory feedback already owns a different outcome")
            return await update(
                tx,
                "personal_memory_feedback_receipts",
                identity,
                {
                    "status": "completed",
                    "outcome_id": outcome,
                    "last_error_code": None,
                    "completed_at": datetime.now(UTC),
                },
            )

    async def fail_feedback(self, identity, code):
        async with self.database.transaction() as tx:
            await tx.execute(
                "UPDATE personal_memory_feedback_receipts SET status='failed',last_error_code=%s,updated_at=now() WHERE id=%s AND status<>'completed'",
                (js_slice(code, 0, 160), identity),
            )
