from datetime import datetime


def requires_acceptance(generation):
    return bool(
        generation and generation["status"] not in {"cancelled", "needs_clarification", "refused"}
    )


def accepted(generation, mission):
    if not generation or not mission:
        return False
    identity = next(
        (
            step.get("metadata", {}).get("generationId")
            for step in reversed(generation.get("steps") or [])
            if isinstance(step.get("metadata", {}).get("generationId"), str)
            and step["metadata"]["generationId"].strip()
        ),
        None,
    )
    return bool(
        mission["requestId"] == generation["requestId"]
        and (not generation.get("projectId") or mission["projectId"] == generation["projectId"])
        and (not identity or mission["generationId"] == identity)
        and mission["missionStatus"] == "completed"
        and mission.get("acceptedReceiptId")
        and mission.get("acceptedReceiptHash")
        and mission.get("acceptedAt")
    )


def reconcile_dispatch(generation, job):
    if not job:
        return generation
    same = bool(generation and generation["requestId"] == job["requestId"])
    queued = job["queuedAt"]
    if isinstance(queued, str):
        queued = datetime.fromisoformat(queued.replace("Z", "+00:00"))
    if not same and generation and generation.get("createdAt"):
        try:
            if datetime.fromisoformat(generation["createdAt"].replace("Z", "+00:00")) > queued:
                return generation
        except (ValueError, TypeError):
            pass
    base = {
        "projectId": job["projectId"],
        "requestId": job["requestId"],
        "cliPreference": job["cliPreference"],
        **(generation if same else {}),
    }
    if job["status"] in {"failed", "interrupted", "cancelled"}:
        return {
            **base,
            "status": "cancelled" if job["status"] == "cancelled" else "failed",
            "error": {"message": job.get("errorMessage") or "研究任务已中断，请重新发起。"},
        }
    if job["status"] == "completed":
        return (
            generation
            if same
            else {
                **base,
                "status": "failed",
                "error": {"message": "研究已结束，但结果状态缺失，请检查任务记录。"},
            }
        )
    if same and generation["status"] == "running" and job["status"] == "running":
        return generation
    active = "planning" if job["stage"] == "planning_data_prefetch" else "agent_execution"
    return {
        **base,
        "status": "running",
        "activeStep": active,
        "steps": [
            {
                "id": active,
                "summary": "正在准备研究上下文与执行输入。"
                if job["status"] == "running"
                else "研究请求已保存，等待 Worker 执行。",
            }
        ],
    }


def terminal_snapshot(
    generation, validation, preview, *, acceptedMission=None, persistedPreviewUrl=None
):
    request = generation.get("requestId") if generation else None
    validation_run = validation.get("runId") if validation else None
    matches = not generation or (
        validation_run == request
        if validation_run
        else generation["status"] in {"completed", "failed"}
    )
    stale = bool(
        validation
        and any(check["id"] == "validation_report_stale" for check in validation["checks"])
    )
    passed = bool(
        validation
        and (validation["passed"] or validation["status"] == "passed")
        and matches
        and not stale
    )
    failed = bool(
        validation
        and (not validation["passed"] or validation["status"] == "failed")
        and matches
        and not stale
    )
    preview_ready = preview["status"] == "running" and bool(preview.get("url"))
    required, mission_accepted = (
        requires_acceptance(generation),
        accepted(generation, acceptedMission),
    )
    satisfied = not required or mission_accepted
    generation_status = generation["status"] if generation else None
    status = "idle"
    if generation_status in {"cancelled", "refused", "needs_clarification"}:
        status = generation_status
    elif generation_status == "failed" and required and not mission_accepted:
        status = "failed"
    elif passed and preview_ready and satisfied:
        status = "ready"
    elif passed:
        status = "preview_pending"
    elif generation_status == "completed" and stale:
        status = "needs_revalidation"
    elif (
        generation
        and validation
        and (stale or not matches)
        or generation_status in {"pending", "running", "repairing"}
    ):
        status = "running"
    elif generation_status == "failed" or failed:
        status = "failed"
    active = generation.get("activeStep") if generation and status == "running" else None
    step = (
        next((step for step in generation.get("steps") or [] if step.get("id") == active), {})
        if generation and status == "running"
        else {}
    )
    summary = (step.get("summary") or "").strip() or None
    return {
        "requestId": request,
        "status": status,
        "activeStep": active,
        "stepSummary": summary,
        "terminal": status
        in {"ready", "needs_revalidation", "failed", "cancelled", "needs_clarification", "refused"},
        "validationStatus": "passed" if passed else "failed" if failed else "pending",
        "validationRunId": validation_run,
        "validationMatchesCurrentRun": matches,
        "missionAcceptanceRequired": required,
        "missionAcceptanceSatisfied": satisfied,
        "acceptedReceiptId": acceptedMission.get("acceptedReceiptId") if mission_accepted else None,
        "previewStatus": preview["status"],
        "previewUrl": preview.get("url") if passed and preview_ready and satisfied else None,
        "previewPort": preview.get("port") if passed and preview_ready and satisfied else None,
        "persistedPreviewUrl": persistedPreviewUrl,
        "errorMessage": (generation.get("error") or {}).get("message") if generation else None,
    }
