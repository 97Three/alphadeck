from datetime import UTC, datetime
from uuid import uuid4

from psycopg.types.json import Jsonb

from .context_use import stable_unique
from .messages import js_slice
from .quota import advisory, one
from .runtime_store import insert, snake, update

PROVIDER = "akep-http-v0.1"
IMMUTABLE = (
    "projectId",
    "requestId",
    "consumerId",
    "integrationScopeSha256",
    "requestedSpaceIds",
    "projectSpaceId",
    "contextPackId",
    "exposureReceiptId",
    "contextDigest",
    "policyEpoch",
    "taskCategory",
    "citations",
    "usageReceipts",
    "acceptedReceiptId",
    "acceptedReceiptSha256",
)


class KnowledgeGrowthError(Exception):
    def __init__(self, code, status, message):
        self.code, self.status = code, status
        super().__init__(message)


def collision():
    raise KnowledgeGrowthError(
        "KNOWLEDGE_USE_COLLISION",
        500,
        "Governed knowledge use attribution collided with a different accepted context.",
    )


def mapped(row):
    if not row:
        return None
    value = {
        key: row[snake(key)]
        for key in (
            *IMMUTABLE,
            "id",
            "provider",
            "feedbackStatus",
            "feedbackEventId",
            "feedbackOutcome",
            "feedbackActorUserId",
            "providerFeedbackReceipts",
            "lastErrorCode",
            "feedbackCompletedAt",
        )
    }
    spaces = value["requestedSpaceIds"]
    if not isinstance(spaces, list) or not all(isinstance(space, str) for space in spaces):
        collision()
    value["requestedSpaceIds"] = stable_unique(spaces)
    if not isinstance(value["citations"], list) or not isinstance(value["usageReceipts"], list):
        collision()
    citations = []
    for item in value["citations"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("locator"), dict)
            or not all(
                isinstance(item.get(key), str) and item[key]
                for key in ("citationId", "payloadDigest", "revisionId", "spaceId")
            )
        ):
            collision()
        citations.append(
            {
                key: item[key]
                for key in ("citationId", "payloadDigest", "locator", "revisionId", "spaceId")
            }
        )
    receipts = []
    for item in value["usageReceipts"]:
        keys = (
            "usageId",
            "exposureReceiptId",
            "spaceId",
            "policyEpoch",
            "createdAt",
            "feedbackUntil",
        )
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) and item[key] for key in keys
        ):
            collision()
        receipts.append({key: item[key] for key in keys})
    value["citations"], value["usageReceipts"] = citations, receipts
    value["feedbackStatus"] = (
        value["feedbackStatus"]
        if value["feedbackStatus"] in {"pending", "completed", "failed"}
        else "awaiting_feedback"
    )
    value["feedbackOutcome"] = (
        value["feedbackOutcome"]
        if value["feedbackOutcome"] in {"helped", "neutral", "harmed"}
        else None
    )
    if value["providerFeedbackReceipts"] is None:
        value["providerFeedbackReceipts"] = []
    elif not isinstance(value["providerFeedbackReceipts"], list):
        collision()
    return value


class KnowledgeRepository:
    def __init__(self, database):
        self.database = database

    async def save(self, input):
        async with self.database.transaction() as tx:
            await advisory(
                tx, "governed-knowledge-use", input["projectId"] + ":" + input["requestId"]
            )
            row = await one(
                tx,
                "SELECT * FROM governed_knowledge_uses WHERE provider=%s AND project_id=%s AND request_id=%s FOR UPDATE",
                (PROVIDER, input["projectId"], input["requestId"]),
            )
            if not row:
                fields = {
                    snake(key): Jsonb(input[key])
                    if key in {"citations", "usageReceipts", "requestedSpaceIds"}
                    else input[key]
                    for key in IMMUTABLE
                }
                row = await insert(
                    tx,
                    "governed_knowledge_uses",
                    {
                        "id": str(uuid4()),
                        "provider": PROVIDER,
                        **fields,
                        "updated_at": datetime.now(UTC),
                    },
                )
            value = mapped(row)
            for key in IMMUTABLE:
                expected, actual = input[key], value[key]
                if key == "requestedSpaceIds":
                    expected, actual = sorted(expected), sorted(actual)
                if actual != expected:
                    collision()
            return value

    async def find(self, project, request):
        return mapped(
            await self.database.fetch_one(
                "SELECT * FROM governed_knowledge_uses WHERE provider=%s AND project_id=%s AND request_id=%s",
                (PROVIDER, project, request),
            )
        )

    async def begin_feedback(self, project, request, actor, event, outcome):
        async with self.database.transaction() as tx:
            row = await one(
                tx,
                "SELECT * FROM governed_knowledge_uses WHERE provider=%s AND project_id=%s AND request_id=%s FOR UPDATE",
                (PROVIDER, project, request),
            )
            if not row:
                raise KnowledgeGrowthError(
                    "KNOWLEDGE_USE_NOT_FOUND",
                    404,
                    "No accepted governed knowledge use exists for this request.",
                )
            if row["feedback_event_id"] is not None and (
                row["feedback_event_id"],
                row["feedback_outcome"],
                row["feedback_actor_user_id"],
            ) != (event, outcome, actor):
                raise KnowledgeGrowthError(
                    "KNOWLEDGE_FEEDBACK_CONFLICT",
                    409,
                    "Governed knowledge feedback was already claimed with different semantics.",
                )
            if row["feedback_status"] == "completed":
                return {"record": mapped(row), "shouldSubmit": False}
            row = await update(
                tx,
                "governed_knowledge_uses",
                row["id"],
                {
                    "feedback_event_id": event,
                    "feedback_outcome": outcome,
                    "feedback_actor_user_id": actor,
                    "feedback_status": "pending",
                    "last_error_code": None,
                },
            )
            return {"record": mapped(row), "shouldSubmit": True}

    async def complete_feedback(self, identity, receipts):
        async with self.database.transaction() as tx:
            row = await one(
                tx, "SELECT * FROM governed_knowledge_uses WHERE id=%s FOR UPDATE", (identity,)
            )
            if not row:
                raise KnowledgeGrowthError(
                    "KNOWLEDGE_USE_NOT_FOUND",
                    404,
                    "No accepted governed knowledge use exists for this request.",
                )
            if (
                row["feedback_status"] == "completed"
                and row["provider_feedback_receipts"] != receipts
            ):
                raise KnowledgeGrowthError(
                    "KNOWLEDGE_FEEDBACK_CONFLICT",
                    409,
                    "Governed knowledge feedback was already claimed with different semantics.",
                )
            return mapped(
                await update(
                    tx,
                    "governed_knowledge_uses",
                    identity,
                    {
                        "feedback_status": "completed",
                        "provider_feedback_receipts": Jsonb(receipts),
                        "last_error_code": None,
                        "feedback_completed_at": datetime.now(UTC),
                    },
                )
            )

    async def fail_feedback(self, identity, code):
        async with self.database.transaction() as tx:
            await tx.execute(
                "UPDATE governed_knowledge_uses SET feedback_status='failed',last_error_code=%s,updated_at=now() WHERE id=%s AND feedback_status<>'completed'",
                (js_slice(code, 0, 160), identity),
            )
