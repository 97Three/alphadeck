import json
import logging
from contextlib import suppress
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .generation_lease import GenerationLease, GenerationLeaseError
from .generation_state import GenerationState
from .http_input import bounded_body, reject_constant
from .lease_session import LeaseSession
from .mission_control import MissionControl, capture_candidate
from .mission_store import MissionError
from .prefetch_transport import MarketTransport, WorkspaceArtifacts
from .realtime import Realtime
from .resource_lock import resource_lock
from .user_requests import RequestBindingError, UserRequests
from .validation_inputs import read_json
from .validation_reports import read_report
from .validation_runtime import ProjectValidator, prepare_validation


def conflict(code, message, mission=None, **extra):
    return JSONResponse(
        {
            "success": False,
            "code": code,
            "error": "Mission state conflict",
            "message": message,
            **(
                {
                    "mission": {
                        "missionId": mission["id"],
                        **{
                            key: mission[key]
                            for key in ("generationId", "requestId", "status", "candidateVersion")
                        },
                    }
                }
                if mission
                else {}
            ),
            **extra,
        },
        status_code=409,
    )


def manual_validation_routes(services, manager_factory, validator_factory=None):
    router = APIRouter()

    @router.get("/api/projects/{project_id}/quant/validation")
    async def get(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.read", project_id)
        manager = manager_factory()
        root = manager.projects.managed_path(
            await manager.projects.get(project_id), must_exist=True
        )
        state = GenerationState(root).read()
        acceptance = (
            await MissionControl(database).store.accepted_snapshot(project_id, state["requestId"])
            if state and state.get("requestId")
            else None
        )
        return {
            "success": True,
            "data": read_report(root),
            "repairPlan": read_json(root, ".data-agent/validation-repair-plan.json"),
            "generationState": state,
            "acceptance": acceptance,
        }

    @router.post("/api/projects/{project_id}/quant/validation")
    async def post(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "agent.run", project_id)
        await auth.require_action(request, "project.source.write", project_id)
        manager = manager_factory()
        root = manager.projects.managed_path(
            await manager.projects.get(project_id), must_exist=True
        )
        content = await bounded_body(request, 65536)
        try:
            body = json.loads(content, parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            body = {}
        body = body if isinstance(body, dict) else {}
        requested = body.get("requestId")
        requested = requested.strip() if isinstance(requested, str) and requested.strip() else None
        requests, control, leases = (
            UserRequests(database),
            MissionControl(database),
            GenerationLease(database),
        )
        try:
            if requested and not await requests.binding(project_id, requested):
                return JSONResponse(
                    {
                        "success": False,
                        "error": "Request does not exist",
                        "code": "REQUEST_NOT_FOUND",
                        "message": "手动验证不能创建或伪造 generation requestId；请使用当前请求，或省略 requestId 执行历史项目验证。",
                    },
                    status_code=404,
                )
            fence = await leases.claim(
                project_id,
                "manual-validation:" + str(uuid4()),
                "manual-api:" + str(uuid4()),
                120000,
                request=requested,
                stage="manual_validation",
            )

            async def release():
                with suppress(GenerationLeaseError):
                    await leases.release(fence)

            async with LeaseSession(
                lambda: leases.heartbeat(fence, 120000),
                ttl_ms=120000,
                interval_ms=30000,
                release=release,
            ) as lease:

                async def authority(operation):
                    lease.healthy()
                    return await leases.fenced(fence, operation)

                artifacts = WorkspaceArtifacts(root, authority)
                state = GenerationState(root, authority=authority)
                validator = (
                    validator_factory(manager)
                    if validator_factory
                    else ProjectValidator(manager, transport=MarketTransport())
                )

                async def run():
                    generation = state.read()
                    if (
                        requested
                        and generation
                        and generation.get("requestId")
                        and generation["requestId"] != requested
                    ):
                        return conflict(
                            "GENERATION_REQUEST_ID_MISMATCH",
                            "手动验证 requestId 必须与当前 generation requestId 一致；如需新一轮验证，请创建新的完整请求。",
                        )
                    identity = requested or (generation or {}).get("requestId")
                    mission = await control.store.read(project_id, identity) if identity else None
                    if mission and mission["status"] in {"completed", "failed", "cancelled"}:
                        completed = mission["status"] == "completed"
                        return conflict(
                            "MISSION_REVALIDATION_REQUIRES_NEW_REQUEST"
                            if completed
                            else "MISSION_TERMINAL",
                            "该请求已经拥有不可变的 accepted receipt；如需重新验证工作区，请创建新的 request。"
                            if completed
                            else f"该 Mission 已处于 {mission['status']} 终态，不能追加候选或验收证据。请创建新的 request。",
                            mission,
                            **(
                                {
                                    "acceptance": await control.store.accepted_snapshot(
                                        project_id, identity
                                    )
                                }
                                if completed
                                else {}
                            ),
                        )
                    if mission and mission["status"] in {"running", "repairing"}:
                        return conflict(
                            "MISSION_BUSY",
                            "该 Mission 仍在执行或验收中，手动验证不能并发读取或封存正在变化的工作区。请等待当前阶段结束后重试。",
                            mission,
                        )
                    if mission and mission["status"] == "verifying":
                        async with control.verification(mission, root):
                            pass
                        mission = await control.store.read(project_id, identity)
                    if mission:
                        await prepare_validation(project_id, artifacts, validator.transport)
                        if mission["status"] == "repair_required":
                            mission = await control.store.repairing(mission)
                        async with resource_lock(
                            root,
                            {"purpose": "manual_candidate_sealing"},
                            recover_dead_local_owner=True,
                        ):
                            sealed = await control.store.candidate(
                                mission,
                                capture_candidate(
                                    root,
                                    source="workspace_recovery",
                                    source_request_id=identity,
                                    summary="手动验证前，平台基于可信准备后的当前工作区封存恢复候选。",
                                ),
                            )
                            mission = sealed["mission"]

                    async def step(key, status, summary, **options):
                        if identity:
                            await state.update(
                                project_id, identity, key, status, summary, **options
                            )

                    await step(
                        "validation",
                        "running",
                        "手动触发 Mission 候选自动验证。" if mission else "手动触发自动验证。",
                    )
                    if not mission:
                        report = await validator.validate(project_id, artifacts, requested)
                        preview = await manager.start(project_id) if report["passed"] else None
                        await step(
                            "validation",
                            "success" if report["passed"] else "failed",
                            "手动验证通过。" if report["passed"] else "手动验证未通过。",
                        )
                        return {
                            "success": True,
                            "data": report,
                            "repairPlan": read_json(
                                root, ".data-agent/validation-repair-plan.json"
                            ),
                            "preview": preview,
                        }
                    preview, evidence = None, None
                    try:
                        async with control.verification(mission, root) as verification:
                            report = await verification.run(
                                lambda: validator.validate(
                                    project_id, artifacts, identity, prepared=True
                                )
                            )
                            if report["passed"]:
                                await step(
                                    "preview", "running", "验证报告通过，正在启动待验收的持久预览。"
                                )
                                try:
                                    preview = await verification.run(
                                        lambda: manager.start(project_id)
                                    )
                                    await step(
                                        "preview",
                                        "success",
                                        "持久预览已就绪，等待 EvidenceVerifier 验收。",
                                        metadata={
                                            "previewUrl": preview["url"],
                                            "previewPort": preview["port"],
                                        },
                                    )
                                except Exception as cause:
                                    await step(
                                        "preview",
                                        "failed",
                                        "持久预览启动失败，交由 EvidenceVerifier 记录基础设施判定。",
                                        error_message=str(cause),
                                    )
                            evidence = await verification.verify(
                                {"url": preview["url"], "port": preview["port"]}
                                if preview
                                else {"url": "http://127.0.0.1:1", "port": 1}
                            )
                        decision, receipt, final = (
                            evidence["decision"],
                            evidence["receipt"],
                            evidence["mission"],
                        )
                        accepted = (
                            decision["verdict"] == "accepted"
                            and final["status"] == "completed"
                            and receipt["receiptType"] == "acceptance"
                            and receipt["verdict"] == "accepted"
                            and final["acceptedReceiptId"] == receipt["id"]
                        )
                        if decision["verdict"] == "accepted" and not accepted:
                            raise ValueError(
                                "EvidenceVerifier returned accepted without a committed acceptance receipt."
                            )
                        await step(
                            "evidence_verification",
                            "success" if accepted else "failed",
                            "当前候选已获得独立 accepted receipt。"
                            if accepted
                            else "证据验收未通过：" + decision["verdict"] + "。",
                        )
                        if accepted:
                            if not report["passed"] or not preview:
                                raise ValueError(
                                    "Mission acceptance requires a passed report and a ready persistent preview."
                                )
                            await step(
                                "completed",
                                "success",
                                "手动恢复链路已完成独立证据验收。",
                                run_status="completed",
                                metadata={
                                    "acceptedReceiptId": receipt["id"],
                                    "acceptedReceiptSha256": receipt["receiptHash"],
                                },
                            )
                            await Realtime(database).publish(
                                project_id,
                                {
                                    "type": "status",
                                    "data": {
                                        "status": "preview_ready",
                                        "requestId": identity,
                                        "metadata": {
                                            "previewUrl": preview["url"],
                                            "previewPort": preview["port"],
                                            "validationPassed": True,
                                            "acceptedReceiptId": receipt["id"],
                                        },
                                    },
                                },
                            )
                        elif preview:
                            await manager.stop(project_id)
                            preview = None
                        return {
                            "success": True,
                            "data": report,
                            "repairPlan": read_json(
                                root, ".data-agent/validation-repair-plan.json"
                            ),
                            "preview": preview,
                            "acceptance": {
                                "required": True,
                                "satisfied": accepted,
                                "verdict": decision["verdict"],
                                "reasonCodes": decision["reasonCodes"],
                                "failedCheckIds": decision["failedCheckIds"],
                                "missionId": final["id"],
                                "generationId": final["generationId"],
                                "missionStatus": final["status"],
                                "candidateVersion": final["candidateVersion"],
                                "receiptId": receipt["id"],
                                "receiptSha256": receipt["receiptHash"],
                            },
                        }
                    except Exception:
                        if preview:
                            with suppress(Exception):
                                await manager.stop(project_id)
                        raise

                return await lease.run(run)
        except RequestBindingError:
            return conflict("REQUEST_PROJECT_MISMATCH", "Request ID belongs to a different project")
        except GenerationLeaseError as cause:
            return conflict(
                cause.code, cause.message, activeRequestId=cause.request_id, activeStage=cause.stage
            )
        except MissionError as cause:
            return conflict(cause.code, str(cause))
        except Exception:
            logging.exception("Manual validation failed for %s", project_id)
            return JSONResponse(
                {
                    "success": False,
                    "error": "Failed to run quant validation",
                    "message": "Internal validation error",
                },
                status_code=500,
            )

    return router
