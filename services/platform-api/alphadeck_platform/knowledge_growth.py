import re
from datetime import UTC, datetime

from .integration_config import integration_scope
from .knowledge_repository import KnowledgeGrowthError, KnowledgeRepository
from .knowledge_service import KnowledgeService
from .messages import js_length, js_slice


def identifier(value, label, maximum=255):
    value = value.strip()
    if not value or js_length(value) > maximum or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        raise KnowledgeGrowthError("INVALID_KNOWLEDGE_FEEDBACK", 400, label + " is invalid.")
    return value


def feedback_open(record):
    if record["feedbackStatus"] == "completed":
        return False
    for receipt in record["usageReceipts"]:
        try:
            if datetime.fromisoformat(
                receipt["feedbackUntil"].replace("Z", "+00:00")
            ) > datetime.now(UTC):
                return True
        except (TypeError, ValueError):
            continue
    return False


def attribution(record):
    return {
        "requestId": record["requestId"],
        "citationCount": len(record["citations"]),
        "revisionCount": len({item["revisionId"] for item in record["citations"]}),
        "spaceCount": len({item["spaceId"] for item in record["citations"]}),
        "feedbackStatus": record["feedbackStatus"],
        "feedbackOutcome": record["feedbackOutcome"],
        "feedbackAvailable": feedback_open(record),
    }


class KnowledgeGrowth:
    def __init__(self, database, *, repository=None, service=None, scope_factory=integration_scope):
        self.repository, self.service = (
            repository or KnowledgeRepository(database),
            service,
        )
        self.scope = scope_factory

    async def persist_accepted(
        self, project, request, category, capsule, usage, receipt_id, receipt_sha
    ):
        if not capsule or usage["status"] != "recorded" or not usage["usageReceipts"]:
            return None
        scope = self.scope(project)
        if (
            capsule["integrationScopeSha256"] != scope["scopeSha256"]
            or capsule["consumerId"] != scope["consumerId"]
            or sorted(capsule["requestedSpaceIds"]) != scope["knowledge"]["requestedSpaceIds"]
        ):
            raise KnowledgeGrowthError(
                "INVALID_KNOWLEDGE_FEEDBACK",
                400,
                "Governed knowledge capsule is outside the accepted project integration scope.",
            )
        if any(
            item["spaceId"] not in capsule["requestedSpaceIds"]
            for item in [*capsule["citations"], *usage["usageReceipts"]]
        ):
            raise KnowledgeGrowthError(
                "INVALID_KNOWLEDGE_FEEDBACK",
                400,
                "Governed knowledge attribution contains a Space outside the accepted project scope.",
            )
        receipt_sha = receipt_sha.strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", receipt_sha):
            raise KnowledgeGrowthError(
                "INVALID_KNOWLEDGE_FEEDBACK", 400, "Accepted receipt SHA-256 is invalid."
            )
        value = await self.repository.save(
            {
                "projectId": identifier(project, "Project ID"),
                "requestId": identifier(request, "Request ID"),
                **{
                    key: capsule[key]
                    for key in (
                        "consumerId",
                        "integrationScopeSha256",
                        "requestedSpaceIds",
                        "projectSpaceId",
                        "contextPackId",
                        "exposureReceiptId",
                        "contextDigest",
                        "policyEpoch",
                    )
                },
                "taskCategory": js_slice(category.strip(), 0, 255) or "quant-research",
                "citations": [
                    {
                        key: item[key]
                        for key in (
                            "citationId",
                            "payloadDigest",
                            "locator",
                            "revisionId",
                            "spaceId",
                        )
                    }
                    for item in capsule["citations"]
                ],
                "usageReceipts": usage["usageReceipts"],
                "acceptedReceiptId": identifier(receipt_id, "Accepted receipt ID"),
                "acceptedReceiptSha256": receipt_sha,
            }
        )
        return attribution(value)

    async def find(self, project, request):
        record = await self.repository.find(
            identifier(project, "Project ID"), identifier(request, "Request ID")
        )
        if not record:
            raise KnowledgeGrowthError(
                "KNOWLEDGE_USE_NOT_FOUND",
                404,
                "No accepted governed knowledge use exists for this request.",
            )
        return attribution(record)

    async def feedback(self, project, request, actor, event, outcome):
        if outcome not in {"helped", "neutral", "harmed"}:
            raise KnowledgeGrowthError(
                "INVALID_KNOWLEDGE_FEEDBACK", 400, "Knowledge feedback outcome is invalid."
            )
        claim = await self.repository.begin_feedback(
            identifier(project, "Project ID"),
            identifier(request, "Request ID"),
            identifier(actor, "Actor user ID"),
            identifier(event, "Feedback event ID"),
            outcome,
        )
        value = claim["record"]
        if not claim["shouldSubmit"]:
            return attribution(value)
        if not feedback_open(value):
            await self.repository.fail_feedback(value["id"], "KNOWLEDGE_FEEDBACK_EXPIRED")
            raise KnowledgeGrowthError(
                "KNOWLEDGE_FEEDBACK_EXPIRED",
                503,
                "Governed knowledge outcome could not be recorded.",
            )
        result = await (self.service or KnowledgeService()).record_feedback(
            {
                "citations": value["citations"],
                "contextDigest": value["contextDigest"],
                "usage": {"status": "recorded", "usageReceipts": value["usageReceipts"]},
                "requestId": value["requestId"],
                "taskCategory": value["taskCategory"],
                "eventId": value["feedbackEventId"],
                "outcome": outcome,
                "acceptedReceiptId": value["acceptedReceiptId"],
                "acceptedReceiptSha256": value["acceptedReceiptSha256"],
            }
        )
        if result["status"] != "recorded":
            code = result.get("failureCode") or "KNOWLEDGE_FEEDBACK_UNAVAILABLE"
            await self.repository.fail_feedback(value["id"], code)
            raise KnowledgeGrowthError(
                code, 503, "Governed knowledge outcome could not be recorded."
            )
        return attribution(
            await self.repository.complete_feedback(value["id"], result["feedbackReceipts"])
        )
