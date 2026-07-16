"""Generation, bounded repair, persistent preview and independent Mission acceptance."""

import asyncio
import inspect
import logging
from contextlib import suppress

from .agent_executor import AgentExecutionError
from .generation_state import GenerationState
from .messages import insert_message
from .mission_control import MissionControl, capture_candidate
from .mission_store import MissionError
from .platform_recovery import recovery_eligible, repair_platform, restore_template, stalled_repair
from .prefetch_market import timestamp
from .provenance import workspace_snapshot
from .resource_lock import resource_lock
from .user_requests import UserRequests
from .validation_repair import failed_checks, repair_instruction
from .validation_runtime import prepare_validation


class GenerationValidation:
    def __init__(
        self,
        database,
        validator,
        previews,
        executor,
        *,
        finish=None,
        publish=None,
        on_accepted=None,
        rewriter=None,
    ):
        self.database, self.validator, self.previews, self.executor = (
            database,
            validator,
            previews,
            executor,
        )
        self.control, self.requests = MissionControl(database), UserRequests(database)
        self.finish, self.publish, self.on_accepted, self.rewriter = (
            finish,
            publish,
            on_accepted,
            rewriter,
        )

    async def run(
        self,
        project,
        request,
        mission,
        artifacts,
        instruction,
        model,
        execution,
        *,
        actor=None,
        conversation=None,
    ):
        state = GenerationState(artifacts.root, authority=artifacts.authority)
        active_child = None

        async def step(key, status, summary, **options):
            return await state.update(project, request, key, status, summary, **options)

        async def finish(status, code=None, message=None, *, preview_url=None):
            if self.finish:
                await self.finish(project, request, status, code=code, message=message)
            if self.publish:
                await self.publish(
                    {
                        "stage": 5,
                        "requestId": request,
                        "status": status,
                        **({"previewUrl": preview_url} if preview_url else {}),
                        **(
                            {"failureReason": message}
                            if status == "failed"
                            else {"cancelledReason": message}
                            if status == "cancelled"
                            else {}
                        ),
                    }
                )

        async def active():
            if await self.requests.cancelled(project, request):
                raise AgentExecutionError("RUN_CANCELLED", "用户暂停了当前任务")

        async def fail(code, message, *, cancelled=False):
            nonlocal mission
            current = await self.control.load(project, request, mission_id=mission["id"])
            status = "cancelled" if cancelled else "failed"
            mission = await self.control.store.terminal(
                current,
                status,
                code,
                message,
                expected_version=current["version"],
                expected_status=current["status"],
            )
            await step("validation", "failed", message, run_status=status, error_message=message)
            await self.requests.status(project, request, status, message)
            if active_child:
                await self.requests.status(project, active_child, "failed", message)
            await finish(status, code, message)
            return {"status": status, "mission": mission, "code": code}

        async def accepted(result, *, recovered=False):
            nonlocal mission
            mission = result["mission"]
            snapshot = await self.control.store.accepted_snapshot(project, request)
            if (
                not snapshot
                or snapshot["missionStatus"] != "completed"
                or not snapshot["acceptedReceiptId"]
                or not snapshot["acceptedReceiptHash"]
                or not snapshot["previewUrl"]
                or not snapshot["previewPort"]
            ):
                raise ValueError(
                    "Mission completion requires a committed accepted evidence receipt and persistent preview."
                )
            metadata = {
                "missionId": snapshot["missionId"],
                "generationId": snapshot["generationId"],
                "candidateVersion": snapshot["candidateVersion"],
                "acceptedReceiptId": snapshot["acceptedReceiptId"],
                "acceptedReceiptSha256": snapshot["acceptedReceiptHash"],
                "previewUrl": snapshot["previewUrl"],
                "previewPort": snapshot["previewPort"],
                **({"recoveredProjection": True} if recovered else {}),
            }
            await step(
                "completed",
                "success",
                "已从 accepted Mission receipt 恢复完成投影。" if recovered else "生成链路完成。",
                run_status="completed",
                metadata=metadata,
            )
            if not self.publish:
                async with self.database.transaction() as tx:
                    await insert_message(
                        tx,
                        project,
                        "assistant",
                        "看板已生成并通过独立证据验收。",
                        request_id=request,
                        conversation=conversation,
                        identity=f"mission-final:{mission['id']}:{snapshot['acceptedReceiptId']}",
                        ensure=True,
                        metadata={"runtime": "pi", "isPiAgentFinal": True, **metadata},
                    )
            if self.on_accepted:
                with suppress(Exception):
                    await self.on_accepted(snapshot)
            await finish("completed", preview_url=snapshot["previewUrl"])
            return {"status": "completed", "mission": mission, "accepted": snapshot}

        async def validate_candidate(candidate, source_request=None, source="workspace_recovery"):
            nonlocal mission
            await active()
            if self.publish:
                await self.publish({"stage": 4})
            await prepare_validation(
                project, artifacts, self.validator.transport, assert_active=active
            )
            async with resource_lock(
                artifacts.root,
                {"purpose": "candidate_sealing", "projectId": project, "requestId": request},
                recover_dead_local_owner=True,
            ):
                if candidate:
                    candidate = {
                        **candidate,
                        "workspaceSha256": "sha256:" + workspace_snapshot(artifacts.root)["sha256"],
                        "submittedAt": timestamp(),
                    }
                else:
                    candidate = capture_candidate(
                        artifacts.root,
                        source=source,
                        source_request_id=source_request or request,
                        summary="平台基于当前工作区封存验证候选。",
                    )
                sealed = await self.control.store.candidate(mission, candidate)
            mission = sealed["mission"]
            async with self.control.verification(mission, artifacts.root) as verification:
                await step("validation", "running", "开始自动验证生成产物。")
                report = await verification.run(
                    lambda: self.validator.validate(project, artifacts, request, prepared=True)
                )
                await active()
                preview = {"url": "http://127.0.0.1:1", "port": 1}
                if report["passed"]:
                    await step("preview", "running", "自动验证通过，正在启动持久看板预览。")
                    preview = await verification.run(lambda: self.previews.start(project))
                    if (
                        preview.get("status") != "running"
                        or not preview.get("url")
                        or type(preview.get("port")) is not int
                        or preview["port"] <= 0
                    ):
                        raise AgentExecutionError(
                            "MISSION_PREVIEW_FAILED", "自动验证通过，但持久看板预览未就绪。"
                        )
                    await step(
                        "preview",
                        "success",
                        "持久看板预览已通过 HTTP 就绪检查。",
                        metadata={"previewUrl": preview["url"], "previewPort": preview["port"]},
                    )
                await active()
                result = await verification.verify({"url": preview["url"], "port": preview["port"]})
                mission = result["mission"]
                decision = result["decision"]
                await step(
                    "evidence_verification",
                    "success" if decision["verdict"] == "accepted" else "failed",
                    "当前候选的验证、产物哈希与持久预览证据已验收。"
                    if decision["verdict"] == "accepted"
                    else "证据验收未通过：" + decision["verdict"],
                    metadata={
                        "missionId": mission["id"],
                        "generationId": mission["generationId"],
                        "candidateVersion": mission["candidateVersion"],
                        "evidenceReceiptId": result["receipt"]["id"],
                        "evidenceReceiptSha256": result["receipt"]["receiptHash"],
                        "verdict": decision["verdict"],
                        "reasonCodes": decision["reasonCodes"],
                        "failedCheckIds": decision["failedCheckIds"],
                    },
                )
            if decision["verdict"] == "accepted":
                return report, await accepted(result)
            if decision["verdict"] != "repair_required":
                raise AgentExecutionError(
                    "MISSION_EVIDENCE_REJECTED",
                    "证据验收未通过："
                    + ", ".join(decision["reasonCodes"] or [decision["verdict"]]),
                )
            return report, None

        try:
            snapshot = await self.control.store.accepted_snapshot(project, request)
            if snapshot and snapshot["missionStatus"] == "completed":
                # A crash after acceptance must not run the Agent against frozen deliverables again.
                if inspect.iscoroutine(execution):
                    execution.close()
                elif isinstance(execution, asyncio.Future):
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                return await accepted(
                    {"mission": await self.control.load(project, request)}, recovered=True
                )
            candidate = None
            try:
                candidate = await (execution() if callable(execution) else execution)
                await step("agent_execution", "success", "Agent 执行完成，进入自动验证。")
            except asyncio.CancelledError:
                raise
            except AgentExecutionError as cause:
                await active()
                if not cause.repairable_by_validation:
                    return await fail(cause.code, cause.message)
                await step(
                    "agent_execution",
                    "failed",
                    "Agent 执行异常结束，进入验证确认产物状态。",
                    error_message=cause.message,
                )
            except Exception as cause:
                await active()
                await step(
                    "agent_execution",
                    "failed",
                    "Agent 执行异常结束，进入验证确认产物状态。",
                    error_message=str(cause),
                )
            report, complete = await validate_candidate(candidate)
            if complete:
                return complete
            generation = state.read() or {}
            maximum = max(1, generation.get("maxRepairAttempts", 1))
            for attempt in range(1, maximum + 1):
                await active()
                mission = await self.control.store.repairing(mission)
                platform = await repair_platform(
                    project, artifacts, request, instruction, report, rewriter=self.rewriter
                )
                if platform["runPlanRebuilt"]:
                    report, complete = await validate_candidate(None, source="platform_repair")
                    if complete:
                        return complete
                    mission = await self.control.store.repairing(mission)
                previous = [check["id"] for check in failed_checks(report)]
                child = request + "-validation-repair" + ("-" + str(attempt) if attempt > 1 else "")
                active_child = child
                repair = repair_instruction(report, {"originalInstruction": instruction})
                await self.requests.create(
                    child, project, repair, actor=actor, cli="pi", upsert=True
                )
                await self.requests.status(project, child, "processing")
                recorded = await state.increment_repair(project, request)
                await step(
                    "repair",
                    "running",
                    f"第 {recorded}/{maximum} 次自动修复开始。",
                    run_status="repairing",
                    metadata={
                        "repairRequestId": child,
                        "repairAttempt": recorded,
                        "maxRepairAttempts": maximum,
                        "failedChecks": previous,
                    },
                )
                repair_candidate, execution_failed = None, False
                try:
                    repair_candidate = await self.executor.execute(
                        project,
                        repair,
                        child,
                        model=model,
                        profile="repair",
                        parent_request=request,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as cause:
                    execution_failed = True
                    await self.requests.status(project, child, "failed", str(cause))
                    await step(
                        "repair",
                        "failed",
                        f"第 {attempt}/{maximum} 次自动修复执行失败。",
                        error_message=str(cause),
                    )
                await active()
                report, complete = await validate_candidate(repair_candidate, source_request=child)
                if complete:
                    await self.requests.status(project, child, "completed")
                    return complete
                current = [check["id"] for check in failed_checks(report)]
                early = stalled_repair(
                    attempt, maximum, execution_failed, previous, current
                ) and recovery_eligible(report)
                if attempt < maximum and not early:
                    await self.requests.status(
                        project, child, "failed", "修复后仍未通过平台验证，继续下一轮修复。"
                    )
                    continue
                mission = await self.control.store.repairing(mission)
                recovery = await restore_template(project, artifacts, report)
                if recovery["restored"]:
                    report, complete = await validate_candidate(
                        None, source_request=child, source="platform_template_recovery"
                    )
                    if complete:
                        await self.requests.status(
                            project,
                            child,
                            "failed",
                            "Agent 修复候选未通过；平台安全模板已接管并完成 Mission。",
                        )
                        return complete
                if early and attempt < maximum:
                    await self.requests.status(
                        project, child, "failed", "确定性恢复后仍有阻断项，继续受限修复。"
                    )
                    continue
                return await fail(
                    "MISSION_VALIDATION_EXHAUSTED", "自动验证和修复耗尽后仍未通过平台验收。"
                )
        except asyncio.CancelledError:
            # An outer lease loss is recoverable; an unowned worker must not project failure.
            raise
        except MissionError as cause:
            if cause.code == "MISSION_VERIFICATION_LEASE_LOST":
                raise
            snapshot = await self.control.store.accepted_snapshot(project, request)
            if snapshot and snapshot["missionStatus"] == "completed":
                return await accepted(
                    {"mission": await self.control.load(project, request)}, recovered=True
                )
            return await fail(cause.code, str(cause))
        except Exception as cause:
            snapshot = await self.control.store.accepted_snapshot(project, request)
            if snapshot and snapshot["missionStatus"] == "completed":
                return await accepted(
                    {"mission": await self.control.load(project, request)}, recovered=True
                )
            logging.exception("Native generation validation failed for %s", project)
            cancelled = await self.requests.cancelled(project, request)
            return await fail(
                getattr(cause, "code", "MISSION_VALIDATION_PIPELINE_FAILED"),
                str(cause),
                cancelled=cancelled,
            )
