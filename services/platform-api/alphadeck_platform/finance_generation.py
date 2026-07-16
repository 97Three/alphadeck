"""Production finance handler: durable preparation → Agent → independently accepted preview."""

import asyncio
import re
from contextlib import suppress
from uuid import uuid4

from .agent_executor import AgentExecutionError, AgentExecutor
from .context_use import record_acceptance, record_exposure
from .data_prefetch import prefetch_data
from .dispatch import Dispatch, assert_fence, lock_job
from .finance_intent import clarification_continuation, clarification_message
from .finance_planning import write_initial_plan
from .finance_request import parse_preparation
from .generation_envelope import (
    GenerationRuntimeRegistry,
    assert_finance_composition,
    assert_registered_finance,
    create_finance_envelope,
    parse_finance_payload,
    parse_generation_envelope,
)
from .generation_lease import (
    GenerationLease,
    GenerationLeaseError,
    check_fence,
    database_now,
    lock_lease,
)
from .generation_state import GenerationState
from .generation_validation import GenerationValidation
from .integration_config import integration_scope
from .knowledge_growth import KnowledgeGrowth
from .knowledge_service import KnowledgeService, write_knowledge_evidence
from .lease_session import LeaseSession
from .llm_config import project_llm_config
from .memory_service import MemoryService
from .messages import insert_message, serialize_message
from .mission_control import MissionControl, capture_candidate
from .prefetch_transport import MarketTransport, WorkspaceArtifacts
from .projects import settings_record
from .provenance import js_json
from .quota import Quota, QuotaError, one
from .realtime import Realtime
from .scaffold import ensure_scaffold
from .user_requests import UserRequests
from .validation_inputs import read_plan
from .validation_runtime import ProjectValidator
from .workspace_progress import WorkspaceProgress


def prefetched_selection(plan, prefetch, instruction=None):
    normalized = re.sub(r"\s+", "", instruction if instruction is not None else plan["question"])
    return bool(
        not prefetch["skipped"]
        and not plan["symbols"]
        and (plan.get("visualization") or {}).get("templateId") == "stock-selection"
        and re.search(r"股票|个股|A股|全A|股票池", normalized)
        and re.search(
            r"全A|A股股票池|股票池|选股|筛选|候选|短线候选|次日|明日|明天|今日|今天|要买|买股|买入策略|短线|推荐\d*(?:只|个)?(?:股票|个股)|(?:股票|个股).{0,12}推荐|推荐.{0,18}(?:股票|个股)",
            normalized,
        )
    )


class FinanceGenerationRuntime:
    def __init__(
        self,
        database,
        projects,
        previews,
        *,
        executor=None,
        validator=None,
        transport=None,
        memory=None,
        knowledge=None,
        scope_factory=integration_scope,
        rewriter=None,
        prefetcher=prefetch_data,
        ttl_ms=120000,
        heartbeat_ms=30000,
    ):
        self.database, self.projects, self.previews = database, projects, previews
        self.transport = transport or MarketTransport()
        self.executor = executor or AgentExecutor(projects)
        self.validator = validator or ProjectValidator(previews, transport=self.transport)
        self.memory, self.knowledge = memory, knowledge
        self.scope, self.rewriter, self.prefetcher = scope_factory, rewriter, prefetcher
        self.ttl, self.interval = ttl_ms, heartbeat_ms
        self.dispatch, self.leases = Dispatch(database), GenerationLease(database)
        self.requests, self.control, self.realtime = (
            UserRequests(database),
            MissionControl(database),
            Realtime(database),
        )

    def registry(self):
        from .finance_catalog import FinanceCatalog

        profile = FinanceCatalog().data["profile"]["id"]
        return GenerationRuntimeRegistry().register(profile, self.execute)

    async def execute(self, job, *, fence=None):
        envelope = parse_generation_envelope(job["executionEnvelope"])
        assert_registered_finance(envelope)
        preparing = (
            isinstance(envelope["payload"], dict)
            and envelope["payload"].get("phase") == "preparation"
        )
        payload = (
            parse_preparation(envelope["payload"]) if preparing else parse_finance_payload(envelope)
        )
        project, request = job["projectId"], job["requestId"]
        if (
            not fence
            or any(fence.get(key) != job[key] for key in ("jobId", "projectId", "requestId"))
            or envelope["scope"]["projectId"] != project
            or envelope["scope"]["workspaceId"] != project
            or envelope["scope"]["requestId"] != request
            or job["selectedModel"] != payload["selectedModel"]
            or job["cliPreference"] != payload["cliPreference"]
        ):
            raise ValueError("Finance handler identity does not match its durable dispatch lease.")
        scope = self.scope(project)
        if any(
            envelope["scope"][key] != expected
            for key, expected in (
                ("integrationScopeSha256", scope["scopeSha256"]),
                ("consumerId", scope["consumerId"]),
                ("tenantId", scope["memory"]["tenantId"]),
            )
        ):
            raise ValueError("Finance integration scope changed after dispatch.")
        if not await self.requests.binding(project, request, payload["actorUserId"]):
            raise ValueError("Finance request actor does not match its durable binding.")
        row = await self.projects.get(project)
        composition = envelope["composition"]
        if (
            row["agent_profile_id"] != composition["profile"]["id"]
            or row["agent_profile_version"] != composition["profile"]["version"]
            or not preparing
            and row["data_agent_composition_sha256"] != composition["sha256"]
        ):
            raise ValueError("Finance project composition does not match the dispatch envelope.")
        root = self.projects.managed_path(row, must_exist=True)
        await self.dispatch.read_claimed(fence)
        if await self.requests.cancelled(project, request):
            await self.dispatch.cancel(project, request, "用户暂停了当前任务")
            return {"status": "cancelled"}
        outer = await self.leases.claim(
            project,
            "generation:" + str(uuid4()),
            fence["leaseOwner"],
            self.ttl,
            request=request,
            stage="planning_data_prefetch" if preparing else "agent_execution",
        )

        async def release():
            with suppress(GenerationLeaseError):
                await self.leases.release(outer)

        async with LeaseSession(
            lambda: self.leases.heartbeat(outer, self.ttl),
            ttl_ms=self.ttl,
            interval_ms=self.interval,
            release=release,
        ) as lease:

            async def active():
                lease.healthy()
                await self.dispatch.read_claimed(fence)
                if await self.requests.cancelled(project, request):
                    raise AgentExecutionError("RUN_CANCELLED", "用户暂停了当前任务")

            async def authority(operation):
                lease.healthy()
                async with self.database.transaction() as tx:
                    # Same parent-first lock order as enqueue/delete; no job → project inversion.
                    if not await one(
                        tx, "SELECT id FROM projects WHERE id=%s FOR UPDATE", (project,)
                    ):
                        raise ValueError("Generation project no longer exists")
                    check_fence(await lock_lease(tx, project), outer, await database_now(tx))
                    assert_fence(
                        await lock_job(tx, project, request), fence, await database_now(tx)
                    )
                    result = await operation(tx)
                    check_fence(await lock_lease(tx, project), outer, await database_now(tx))
                    assert_fence(
                        await lock_job(tx, project, request), fence, await database_now(tx)
                    )
                    return result

            artifacts = WorkspaceArtifacts(root, authority)
            state = GenerationState(root, authority=authority)
            mission = None

            async def finish(_project, _request, status, *, code=None, message=None):
                if (_project, _request) != (project, request):
                    raise ValueError("Cannot finalize another generation request")
                if status == "cancelled":
                    return await self.dispatch.cancel(project, request, message)
                await active()
                return await self.dispatch.finish(
                    project, request, status, fence=fence, code=code, message=message
                )

            publish = WorkspaceProgress(self.database, project, request, payload["conversationId"])

            async def run():
                nonlocal envelope, payload, mission
                if preparing:
                    prepared = await self.prepare(
                        job, envelope, payload, row, artifacts, state, scope, active, publish, fence
                    )
                    if prepared is None:
                        await finish(project, request, "completed")
                        return {"status": "completed", "preparationOnly": True}
                    envelope, payload = prepared, parse_finance_payload(prepared)
                await active()
                plan = read_plan(root)
                mission = await self.control.load(
                    project,
                    request,
                    mission_id=payload["missionId"],
                    generation_id=payload["generationId"],
                )
                if not plan or plan["runId"] != request or plan["status"] != "planned":
                    raise ValueError("Execution requires the current planned finance run.")
                assert_finance_composition(envelope, plan, await self.control.store.spec(mission))
                memory = self.memory or MemoryService(self.database, scope_factory=self.scope)
                personalization = await memory.expose(
                    project, payload["memorySubjectId"], request, payload["personalizationRecall"]
                )
                knowledge = (
                    None
                    if payload["usePrefetchedSelectionDashboard"]
                    else payload["governedKnowledgePreparation"]["capsule"]
                )
                await active()
                await record_exposure(
                    artifacts, project, request, scope, personalization, knowledge
                )

                async def accepted(snapshot):
                    service = self.knowledge or KnowledgeService()
                    usage = await service.record_usage(
                        knowledge, request, payload["governedKnowledgeTaskCategory"]
                    )
                    await write_knowledge_evidence(
                        artifacts, request, payload["governedKnowledgePreparation"], usage
                    )
                    await KnowledgeGrowth(
                        self.database, service=service, scope_factory=self.scope
                    ).persist_accepted(
                        project,
                        request,
                        payload["governedKnowledgeTaskCategory"],
                        knowledge,
                        usage,
                        snapshot["acceptedReceiptId"],
                        snapshot["acceptedReceiptHash"],
                    )
                    await record_acceptance(
                        artifacts,
                        project,
                        request,
                        {
                            "missionId": snapshot["missionId"],
                            "acceptedReceiptId": snapshot["acceptedReceiptId"],
                            "acceptedReceiptSha256": snapshot["acceptedReceiptHash"],
                        },
                        usage,
                        scope=scope,
                    )

                async def execution():
                    await active()
                    await state.update(
                        project, request, "agent_execution", "running", "正在生成可验证的量化看板。"
                    )
                    if payload["usePrefetchedSelectionDashboard"]:
                        return capture_candidate(
                            root,
                            source="platform_prefetch",
                            source_request_id=request,
                            summary="平台已基于预取数据生成确定性选股看板候选。",
                        )
                    return await self.executor.execute(
                        project,
                        payload["effectiveInstruction"],
                        request,
                        model=payload["selectedModel"],
                        images=payload["processedImages"],
                        personalization=personalization,
                        knowledge=knowledge,
                    )

                return await GenerationValidation(
                    self.database,
                    self.validator,
                    self.previews,
                    self.executor,
                    finish=finish,
                    publish=publish,
                    on_accepted=accepted,
                    rewriter=self.rewriter,
                ).run(
                    project,
                    request,
                    mission,
                    artifacts,
                    payload["userVisibleInstructionForRepair"],
                    payload["selectedModel"],
                    execution,
                    actor=payload["actorUserId"],
                    conversation=payload["conversationId"],
                )

            try:
                return await lease.run(run)
            except asyncio.CancelledError:
                raise
            except Exception as cause:
                # A cancelled/replaced owner cannot publish a late failure or edit the workspace.
                await active()
                message = str(cause)
                current = await self.control.store.read(project, request)
                if current and current["status"] not in {"completed", "failed", "cancelled"}:
                    await self.control.store.terminal(
                        current,
                        "failed",
                        getattr(cause, "code", "RESEARCH_PREPARATION_FAILED"),
                        message,
                    )
                await state.update(
                    project,
                    request,
                    "planning" if preparing else "agent_execution",
                    "failed",
                    "研究执行失败。",
                    run_status="failed",
                    error_message=message,
                )
                await self.requests.status(project, request, "failed", message)
                await publish({"stage": 5, "failureReason": message})
                await finish(
                    project,
                    request,
                    "failed",
                    code=getattr(cause, "code", "RESEARCH_PREPARATION_FAILED"),
                    message=message,
                )
                return {"status": "failed", "error": message}

    async def prepare(
        self, job, envelope, payload, row, artifacts, state, scope, active, publish, fence
    ):
        project, request = job["projectId"], job["requestId"]
        if not await self.requests.status(project, request, "processing"):
            raise ValueError("Research request is no longer active.")
        previous = read_plan(artifacts.root)
        continuation = clarification_continuation(
            {
                "previousPlan": previous,
                "instruction": payload["finalInstruction"],
                "displayInstruction": payload["displayInstruction"],
                "reset": payload["isInitialPrompt"],
            }
        )
        effective = (
            "\n\n".join(
                filter(
                    None,
                    [continuation["resolvedInstruction"], payload["imageAttachmentInstruction"]],
                )
            )
            if continuation
            else payload["finalInstruction"]
        )
        display = (
            continuation["displayInstruction"] if continuation else payload["displayInstruction"]
        )
        memory = self.memory or MemoryService(self.database, scope_factory=self.scope)
        recall = await memory.recall(
            project,
            payload["memorySubjectId"],
            request,
            display or effective,
            payload["capabilityId"],
        )
        generation = await state.start(
            project, request, payload["finalInstruction"], cli="pi", model=payload["selectedModel"]
        )
        await state.update(
            project, request, "planning", "running", "正在整理问题、标的、周期和验收规则。"
        )
        quota, reservation = Quota(self.database), None
        actor = payload["actorUserId"]
        try:
            if actor:
                decision = await quota.reserve(
                    actor,
                    "query_rewrite.llm.daily",
                    1,
                    f"chat-query-rewrite:{actor}:{request}:reservation",
                    project_id=project,
                )
                if not decision["allowed"]:
                    raise QuotaError(
                        "QUOTA_EXCEEDED", "Query Rewrite quota exceeded.", decision=decision
                    )
                reservation = (decision.get("reservation") or {}).get("id")
            plan = await write_initial_plan(
                {
                    "projectId": project,
                    "requestId": request,
                    "instruction": display.strip() or effective,
                    "capabilityId": payload["capabilityId"],
                    "capabilitySource": payload["capabilitySelectionSource"],
                    "hasImageAttachments": bool(payload["processedImages"]),
                    "previousPlan": previous,
                    "llmModel": payload["selectedModel"],
                },
                artifacts,
                rewriter=self.rewriter,
                assert_active=active,
            )
            llm = plan["queryRewrite"]["execution"]["llm"]
            if reservation:
                await quota.settle(
                    reservation,
                    int(llm["attempted"]),
                    "query_rewrite",
                    source_id=request,
                    event_key=f"chat-query-rewrite:{actor}:{request}:request",
                    metadata={
                        "status": llm["status"],
                        "strategy": plan["queryRewrite"]["execution"]["strategy"],
                    },
                )
                reservation = None
            if actor and llm["attempted"] and (llm.get("usage") or {}).get("totalTokens", 0) > 0:
                await quota.record_usage(
                    actor,
                    "llm.total_tokens.monthly",
                    llm["usage"]["totalTokens"],
                    f"chat-query-rewrite:{actor}:{request}:tokens",
                    "query_rewrite",
                    source_id=request,
                    project_id=project,
                    metadata={"provider": llm["provider"], "model": llm["model"], **llm["usage"]},
                )
        finally:
            if reservation:
                await quota.release(reservation)
        await active()
        if plan.get("failure"):
            raise AgentExecutionError(plan["failure"]["code"], plan["failure"]["message"])
        await publish({"stage": 1, "runPlan": plan})
        if plan["status"] in {"refused", "needs_clarification"}:
            refused = plan["status"] == "refused"
            content = (
                plan["refusal"]["message"]
                if refused
                else clarification_message(plan["clarification"])
            )
            await state.update(
                project, request, "planning", "warning", content, run_status=plan["status"]
            )
            async with self.database.transaction() as tx:
                message = await insert_message(
                    tx,
                    project,
                    "assistant",
                    content,
                    request_id=request,
                    conversation=payload["conversationId"],
                    identity="preparation-final:" + request,
                    ensure=True,
                    metadata={
                        "type": "intent_refusal" if refused else "intent_clarification",
                        "isMissionFinal": True,
                        "progressStatus": "refused" if refused else "clarification",
                        "runPlanPath": ".data-agent/finance-run-plan.json",
                        "refusal" if refused else "clarification": plan[
                            "refusal" if refused else "clarification"
                        ],
                    },
                )
            await self.requests.status(project, request, "completed")
            await self.realtime.publish(
                project, {"type": "message", "data": serialize_message(message)}
            )
            return None
        knowledge_service = self.knowledge or KnowledgeService()
        knowledge = await knowledge_service.prepare(
            "\n".join(
                [
                    plan["queryRewrite"].get("rewrittenQuery") or plan["question"],
                    "capability: " + plan["capabilityId"],
                    *plan["analysisSteps"][:12],
                ]
            ),
            request,
            scope,
        )
        await write_knowledge_evidence(artifacts, request, knowledge)
        mission = await self.control.create(
            project,
            request,
            plan["queryRewrite"].get("rewrittenQuery") or plan["question"],
            plan,
            generation["maxRepairAttempts"],
        )
        mission = await self.control.store.mark_node(mission, "planning", "passed")
        await state.update(project, request, "planning", "success", "已生成执行计划。")
        await state.update(project, request, "data_prefetch", "running", "正在预取真实数据。")
        mission = await self.control.store.mark_node(mission, "data_prefetch", "running")
        await publish({"stage": 2, "runPlan": plan})
        prefetch = await self.prefetcher(
            plan,
            self.transport,
            artifacts,
            assert_active=active,
            on_progress=lambda progress: publish({"stage": 2, "prefetchProgress": progress}),
        )
        await active()
        await state.update(
            project,
            request,
            "data_prefetch",
            "skipped" if prefetch["skipped"] else "success",
            prefetch["summary"],
        )
        mission = await self.control.store.mark_node(
            mission, "data_prefetch", "skipped" if prefetch["skipped"] else "passed"
        )

        async def scaffold(_tx):
            ensure_scaffold(artifacts.root, project)

        await artifacts.mutate(scaffold)
        await publish({"stage": 3, "runPlan": plan})
        prepared = create_finance_envelope(
            {
                "effectiveInstruction": effective,
                "userVisibleInstructionForRepair": display or payload["finalInstruction"],
                "selectedModel": payload["selectedModel"],
                "cliPreference": "pi",
                "isInitialPrompt": payload["isInitialPrompt"],
                "conversationId": payload["conversationId"],
                "actorUserId": actor,
                "memorySubjectId": payload["memorySubjectId"],
                "processedImages": payload["processedImages"],
                "usePrefetchedSelectionDashboard": prefetched_selection(plan, prefetch, effective),
                "missionId": mission["id"],
                "generationId": mission["generationId"],
                "governedKnowledgeTaskCategory": plan["capabilityId"],
                "personalizationRecall": recall,
                "governedKnowledgePreparation": knowledge,
            },
            project,
            request,
            plan["capabilityId"],
            scope=scope,
        )

        async def lock_composition(tx):
            current = await one(tx, "SELECT * FROM projects WHERE id=%s FOR UPDATE", (project,))
            settings = settings_record(current["settings"])
            settings["llm"] = project_llm_config(payload["selectedModel"])
            composition = prepared["composition"]
            settings["dataAgent"] = {
                "profileId": composition["profile"]["id"],
                "profileVersion": composition["profile"]["version"],
                "capabilityId": composition["capability"]["id"],
                "compositionSha256": composition["sha256"],
            }
            await tx.execute(
                "UPDATE projects SET agent_profile_id=%s,agent_profile_version=%s,data_agent_composition_sha256=%s,settings=%s,selected_model=%s,preferred_cli='pi',last_active_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s",
                (
                    composition["profile"]["id"],
                    composition["profile"]["version"],
                    composition["sha256"],
                    js_json(settings),
                    payload["selectedModel"],
                    project,
                ),
            )

        await artifacts.authority(lock_composition)
        await active()
        await self.dispatch.checkpoint(fence, prepared)
        return prepared
