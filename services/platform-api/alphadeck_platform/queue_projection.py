from .dispatch import Dispatch
from .generation_state import now
from .sessions import iso


async def queue_projection(database, project, limit=50):
    jobs = await Dispatch(database).jobs(project, limit)
    items = [
        {
            "id": row["id"],
            "projectId": row["project_id"],
            "requestId": row["request_id"],
            "status": {
                "pending": "queued",
                "retry_wait": "queued",
                "running": "running",
                "completed": "completed",
                "cancelled": "cancelled",
            }.get(row["status"], "failed"),
            "cliPreference": row["cli_preference"],
            "selectedModel": row["selected_model"],
            "instructionPreview": row["instruction_preview"],
            "queuedAt": iso(row["queued_at"]),
            "startedAt": iso(row["started_at"]) if row["started_at"] else None,
            "completedAt": iso(row["completed_at"]) if row["completed_at"] else None,
            "errorMessage": row["error_message"],
        }
        for row in jobs
    ]
    return {
        "schemaVersion": 1,
        "projectId": project,
        "activeRequestId": next(
            (item["requestId"] for item in items if item["status"] == "running"), None
        ),
        "updatedAt": iso(jobs[0]["updated_at"]) if jobs else now(),
        "items": items,
    }
