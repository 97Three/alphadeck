"""Product metrics use accepted Mission receipts, never a frontend completion marker."""

import math
from collections import Counter
from datetime import UTC, datetime, timedelta

from .evaluation_queue import date
from .sessions import iso


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    return math.floor(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower) + 0.5)


def percentage(numerator, denominator):
    return math.floor(numerator / denominator * 1000 + 0.5) / 10 if denominator > 0 else None


def accepted(mission):
    if not mission or not (receipt := mission.get("acceptedReceipt")):
        return False
    return (
        mission["status"] == "completed"
        and all(
            receipt.get(key) == mission.get(other)
            for key, other in (
                ("id", "acceptedReceiptId"),
                ("missionId", "id"),
                ("candidateVersion", "candidateVersion"),
            )
        )
        and receipt.get("receiptType") == "acceptance"
        and receipt.get("verdict") == "accepted"
    )


def unavailable_first(error):
    return {
        "available": False,
        "observationHours": 24,
        "researchers": 0,
        "maturedResearchers": 0,
        "observingResearchers": 0,
        "acceptedResearchers": 0,
        "completionRate": None,
        "medianDeliveryMs": None,
        "p90DeliveryMs": None,
        "invalidDeliveryTimings": 0,
        "error": error,
    }


def first_research(first_requests, accepted_requests, since, now):
    first = {
        entry["actorUserId"]: parsed
        for entry in first_requests
        if entry.get("actorUserId")
        and (parsed := date(entry["_min"].get("createdAt")))
        and since <= parsed <= now
    }
    mature = {actor: value for actor, value in first.items() if value + timedelta(hours=24) <= now}
    deliveries, invalid = {}, 0
    for request in accepted_requests:
        actor = request.get("actorUserId")
        if actor not in mature:
            continue
        created, completed = date(request["createdAt"]), date(request.get("completedAt"))
        if (
            not created
            or not completed
            or created < mature[actor]
            or completed < created
            or completed > now
        ):
            invalid += 1
            continue
        if completed > mature[actor] + timedelta(hours=24):
            continue
        duration = (completed - mature[actor]).total_seconds() * 1000
        deliveries[actor] = min(deliveries.get(actor, duration), duration)
    return {
        "available": True,
        "observationHours": 24,
        "researchers": len(first),
        "maturedResearchers": len(mature),
        "observingResearchers": len(first) - len(mature),
        "acceptedResearchers": len(deliveries),
        "completionRate": percentage(len(deliveries), len(mature)),
        "medianDeliveryMs": percentile(list(deliveries.values()), 0.5),
        "p90DeliveryMs": percentile(list(deliveries.values()), 0.9),
        "invalidDeliveryTimings": invalid,
        "error": None,
    }


def summarize(requests, reports, generated_at, *, window_days=7, sampled=False, first=None):
    terminal = ("completed", "failed", "cancelled")
    counts = Counter(item["status"] for item in requests)
    missions = [request["agentMission"] for request in requests if request.get("agentMission")]
    completed_missions = sum(mission["status"] == "completed" for mission in missions)
    terminal_missions = sum(mission["status"] in terminal for mission in missions)
    accepted_requests = [request for request in requests if accepted(request.get("agentMission"))]
    durations = []
    for request in accepted_requests:
        created, completed = (
            date(request["createdAt"]),
            date(request["agentMission"].get("completedAt")),
        )
        if created and completed and created <= completed <= generated_at:
            durations.append((completed - created).total_seconds() * 1000)
    researchers = Counter(item["actorUserId"] for item in requests if item.get("actorUserId"))
    repeat = sum(count >= 2 for count in researchers.values())
    jobs = [
        request["generationJob"]
        for request in requests
        if request.get("generationJob") and request["generationJob"].get("startedAt")
    ]
    waits = []
    for job in jobs:
        queued, started = date(job["queuedAt"]), date(job["startedAt"])
        if queued and started and queued <= started <= generated_at:
            waits.append((started - queued).total_seconds() * 1000)
    terminals = sum(counts[key] for key in terminal)
    return {
        "available": True,
        "generatedAt": iso(generated_at),
        "windowDays": window_days,
        "sampled": sampled,
        "firstResearch": first
        if first is not None
        else unavailable_first("首次研究指标需要全历史首次提交时间。"),
        "summary": {
            "requests": len(requests),
            "activeProjects": len({item["projectId"] for item in requests}),
            "completedRequests": counts["completed"],
            "failedRequests": counts["failed"],
            "cancelledRequests": counts["cancelled"],
            "activeRequests": len(requests) - terminals - counts["needs_clarification"],
            "clarificationRequests": counts["needs_clarification"],
            "acceptedDeliveries": len(accepted_requests),
            "completedMissions": completed_missions,
            "unverifiedCompletedMissions": completed_missions - len(accepted_requests),
            "terminalMissions": terminal_missions,
            "reports": reports,
            "uniqueResearchers": len(researchers),
            "repeatResearchers": repeat,
            "requestCompletionRate": percentage(counts["completed"], terminals),
            "missionAcceptanceRate": percentage(len(accepted_requests), terminal_missions),
            "evidenceCompletenessRate": percentage(len(accepted_requests), completed_missions),
            "repeatResearcherRate": percentage(repeat, len(researchers)),
            "medianDeliveryMs": percentile(durations, 0.5),
            "p90DeliveryMs": percentile(durations, 0.9),
            "p95DeliveryMs": percentile(durations, 0.95),
            "deliveryTimingSamples": len(durations),
            "invalidDeliveryTimings": len(accepted_requests) - len(durations),
            "medianQueueWaitMs": percentile(waits, 0.5),
            "p90QueueWaitMs": percentile(waits, 0.9),
            "queueTimingSamples": len(waits),
            "invalidQueueTimings": len(jobs) - len(waits),
        },
        "error": None,
    }


async def product_dashboard(database, *, enabled=True, now=None, window_days=7):
    now = now or datetime.now(UTC)
    since = now - timedelta(days=window_days)
    error = "数据库已按降级配置停用，产品闭环指标未采集。" if not enabled else None
    try:
        if not enabled:
            raise ValueError(error)
        rows = await database.fetch_all(
            """SELECT r.project_id,r.actor_user_id,r.status,r.created_at,r.completed_at,
            j.queued_at,j.started_at,m.id AS mission_id,m.status AS mission_status,m.candidate_version,m.accepted_receipt_id,m.completed_at AS mission_completed_at,
            e.id AS receipt_id,e.mission_id AS receipt_mission_id,e.candidate_version AS receipt_candidate_version,e.receipt_type,e.verdict
            FROM user_requests r LEFT JOIN agent_generation_jobs j ON j.project_id=r.project_id AND j.request_id=r.id
            LEFT JOIN agent_missions m ON m.project_id=r.project_id AND m.request_id=r.id
            LEFT JOIN agent_evidence_receipts e ON e.id=m.accepted_receipt_id
            WHERE r.created_at >= %s AND r.created_at <= %s ORDER BY r.created_at DESC,r.id DESC LIMIT 10001""",
            (since, now),
        )
        requests = []
        for row in rows:
            mission = (
                {
                    "id": row["mission_id"],
                    "status": row["mission_status"],
                    "candidateVersion": row["candidate_version"],
                    "acceptedReceiptId": row["accepted_receipt_id"],
                    "completedAt": row["mission_completed_at"],
                    "acceptedReceipt": {
                        "id": row["receipt_id"],
                        "missionId": row["receipt_mission_id"],
                        "candidateVersion": row["receipt_candidate_version"],
                        "receiptType": row["receipt_type"],
                        "verdict": row["verdict"],
                    }
                    if row["receipt_id"]
                    else None,
                }
                if row["mission_id"]
                else None
            )
            requests.append(
                {
                    "projectId": row["project_id"],
                    "actorUserId": row["actor_user_id"],
                    "status": row["status"],
                    "createdAt": row["created_at"],
                    "completedAt": row["completed_at"],
                    "agentMission": mission,
                    "generationJob": {"queuedAt": row["queued_at"], "startedAt": row["started_at"]}
                    if row["queued_at"]
                    else None,
                }
            )
        report_count = await database.fetch_one(
            "SELECT count(*) AS count FROM research_reports WHERE report_date >= %s AND report_date <= %s",
            (since, now),
        )
        sampled = len(requests) > 10000
        first = unavailable_first("请求窗口已截断，首次研究指标暂不计算。")
        if not sampled:
            try:
                actors = list(
                    {request["actorUserId"] for request in requests if request["actorUserId"]}
                )
                history = (
                    await database.fetch_all(
                        "SELECT actor_user_id,min(created_at) AS first_at FROM user_requests WHERE actor_user_id=ANY(%s) AND created_at <= %s GROUP BY actor_user_id",
                        (actors, now),
                    )
                    if actors
                    else []
                )
                first = first_research(
                    [
                        {
                            "actorUserId": row["actor_user_id"],
                            "_min": {"createdAt": row["first_at"]},
                        }
                        for row in history
                    ],
                    [
                        {
                            "actorUserId": request["actorUserId"],
                            "createdAt": request["createdAt"],
                            "completedAt": request["agentMission"]["completedAt"],
                        }
                        for request in requests
                        if accepted(request["agentMission"])
                    ],
                    since,
                    now,
                )
            except Exception:
                first = unavailable_first("首次研究历史暂不可用，窗口指标仍可查看。")
        return summarize(
            requests[:10000],
            report_count["count"],
            now,
            window_days=window_days,
            sampled=sampled,
            first=first,
        )
    except Exception:
        error = error or "产品闭环指标暂不可用，请检查数据库连接。"
        result = summarize([], 0, now, window_days=window_days, first=unavailable_first(error))
        result.update(available=False, error=error)
        return result
