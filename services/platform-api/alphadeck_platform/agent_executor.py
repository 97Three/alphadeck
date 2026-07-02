"""Native application composition of planning contracts, Skills, tools and durable Agent runs."""

import asyncio
import json
import os
import re
from contextlib import suppress
from uuid import uuid4

from .agent_prompts import system_prompt, task_prompt, user_prompt
from .build_identity import build_identity
from .config import ROOT
from .context_manager import ContextManager
from .dashboard_inspector import DashboardInspector
from .dashboard_spec import supported
from .deterministic_provider import DeterministicProvider
from .finance_catalog import FinanceCatalog
from .finance_tools import FinanceTools
from .llm_config import env_flag, project_llm_config
from .messages import insert_message, js_length, js_slice
from .mission_control import candidate_from_run
from .phase_graph import classify_prepared_intent, phase_graph
from .prepared_artifacts import assess_prepared
from .provenance import js_json, provenance_hash, workspace_identity, workspace_snapshot
from .provider import ChatProvider
from .recovery import reconcile_expired
from .research_time import research_time_for_plan
from .resource_lock import resource_lock
from .runtime_session import RuntimeSession
from .runtime_store import RuntimeStore
from .skill_compiler import compile_skills
from .tool_approval import DurableApprovalHandler
from .tool_paths import WorkspacePolicy
from .user_requests import UserRequests
from .validation_inputs import read_plan
from .validation_repair import failed_checks, writable_globs
from .validation_reports import read_report
from .visualization import serialize_template

DATA_CHECKS = {"final_data_file", "evidence_files", "artifact_contracts", "dashboard_data_binding"}
DASHBOARD_CHECKS = {
    "vue_build",
    "preview_http_200",
    "visual_presentation",
    "artifact_policy",
    "dashboard_data_binding",
    "chart_presence",
    "market_proxy",
}
SOURCE_PATHS = ["src/App.vue", "src/style.css"]


def positive_env(name, default, *, maximum=9007199254740991, minimum=1):
    value = os.getenv(name, "").strip()
    try:
        parsed = float(value)
        return int(parsed) if parsed.is_integer() and minimum <= parsed <= maximum else default
    except (ValueError, OverflowError):
        return default


class AgentExecutionError(Exception):
    def __init__(self, code, message, *, repairable=False):
        self.code, self.message, self.repairable_by_validation = code, message, repairable
        super().__init__(message)


def execution_contract(root, instruction, *, profile="generation", has_attachments=False):
    if profile not in {"generation", "repair"}:
        raise ValueError("Unknown Agent execution profile")
    plan = read_plan(root)
    capability = (plan or {}).get("requestedCapabilityId") or (plan or {}).get("capabilityId")
    prepared = assess_prepared(root, plan)
    phase = (
        "validation-repair"
        if profile == "repair"
        else "workspace-generation"
        if prepared["ready"] and not has_attachments
        else "data-preparation"
    )
    chosen = (plan or {}).get("visualization") or {}
    visualization = serialize_template(
        capability,
        {
            "instruction": (plan or {}).get("question") or instruction,
            "symbolCount": len((plan or {}).get("symbols", [])),
            "requestedVariantId": chosen.get("variantId"),
            "dataSignals": chosen.get("dataSignals"),
        },
    )
    template, variant = (
        chosen.get("templateId") or visualization["templateId"],
        chosen.get("variantId") or visualization["variantId"],
    )
    standard = (
        classify_prepared_intent(instruction) == "standard"
        and supported(template, variant)
        and prepared["dashboardSpecReady"]
    )
    intent = (
        ("standard" if standard else "custom")
        if profile == "generation" and prepared["ready"] and not has_attachments
        else None
    )
    graph = phase_graph(
        {
            "profile": profile,
            "platformPrepared": prepared["ready"],
            "preparedIntent": intent,
            "hasAttachments": has_attachments,
            "dashboardSpecReady": prepared["dashboardSpecReady"],
        }
    )
    report = read_report(root) if profile == "repair" else None
    if profile == "repair" and (
        not report
        or report.get("status") != "failed"
        or not failed_checks(report)
        or any(check["id"] == "validation_report_stale" for check in report["checks"])
    ):
        raise AgentExecutionError(
            "REPAIR_REPORT_REQUIRED",
            "PI Agent repair 缺少当前平台失败报告或明确失败项，拒绝扩大写权限。",
        )
    failed = {check["id"] for check in failed_checks(report)} if report else set()
    needs_data = bool(failed & DATA_CHECKS)
    needs_dashboard = bool(failed & DASHBOARD_CHECKS or failed - DATA_CHECKS - DASHBOARD_CHECKS)
    selected = (
        [
            *(["data-quality"] if needs_data else []),
            *(["dashboard-visualization"] if needs_dashboard else []),
        ]
        if profile == "repair"
        else [
            *(["data-quality", "image-extraction"] if has_attachments else []),
            "dashboard-visualization",
        ]
        if prepared["ready"]
        else None
    )
    globs = writable_globs(report) if report else None
    if profile == "repair" and not globs:
        raise AgentExecutionError(
            "REPAIR_SCOPE_REQUIRED",
            "PI Agent repair 无法把当前失败安全归因到明确文件，拒绝启动宽权限自动修复。",
        )
    source_writes = bool(globs and any(path.startswith("app/") for path in globs))
    file_writes = profile == "repair" and bool(
        needs_data or any(not path.startswith("app/") for path in globs)
    )
    repair_surface = (
        "custom"
        if profile == "repair"
        and source_writes
        and all(path in SOURCE_PATHS for path in globs)
        and not file_writes
        else None
    )
    runtime_globs = globs if profile == "repair" else SOURCE_PATHS if intent else None
    supplemental = (
        [
            "evidence/image_extraction.json",
            "evidence/data_quality.json",
            "evidence/sources.json",
            "data_file/final/dashboard-data.json",
        ]
        if profile != "repair" and (not prepared["ready"] or has_attachments)
        else []
    )
    options = {
        "profile": profile,
        "profile_allowed_write_globs": runtime_globs,
        "include_default": runtime_globs is None,
        "allowed_write_globs": supplemental,
        "max_output": positive_env("PI_AGENT_TOOL_OUTPUT_CHARS", 6000),
        "include_image_extraction": has_attachments,
        "include_quant_api": not (prepared["ready"] or repair_surface),
        "targeted_reads_only": prepared["ready"] or bool(repair_surface),
        "prepared_surface": intent or repair_surface,
        "include_dashboard_spec": profile != "repair" and intent == "standard",
        "include_dashboard_inspector": not (prepared["ready"] or repair_surface)
        and (profile != "repair" or needs_dashboard),
        "include_semantic_edit": profile != "repair" or source_writes,
        "allowed_mutation_tool_names": [
            *(["write_file", "edit_file"] if file_writes else []),
            *(["semantic_edit"] if source_writes else []),
        ]
        if profile == "repair"
        else None,
        "research_time": research_time_for_plan(plan) if plan else None,
    }
    return {
        "plan": plan,
        "capabilityId": capability,
        "assessment": prepared,
        "phase": phase,
        "preparedIntent": intent,
        "graph": graph,
        "templateId": template,
        "variantId": variant,
        "selectedSkillIds": selected,
        "dashboardContractRequired": profile != "repair" or needs_dashboard,
        "toolOptions": options,
    }


async def bounded_history(database, project, request=None):
    rows = await database.fetch_all(
        "SELECT * FROM (SELECT * FROM messages WHERE project_id=%s AND message_type='chat' ORDER BY created_at DESC LIMIT 30) history ORDER BY created_at",
        (project,),
    )
    selected, characters = [], 0
    for row in reversed(rows):
        if request and row["request_id"] == request:
            continue
        if row["role"] not in {"user", "assistant"}:
            continue
        if row["role"] == "assistant":
            if (
                row["cli_source"] == "validator"
                or row["request_id"]
                and re.search(r"-validation-repair(?:-\d+)?$", row["request_id"])
            ):
                continue
            try:
                metadata = json.loads(row["metadata_json"] or "null")
                metadata = metadata if isinstance(metadata, dict) else {}
            except ValueError:
                metadata = {}
            if (
                metadata.get("isPiAgentIntermediateTurn") is True
                or row["cli_source"] == "pi"
                and row["request_id"]
                and metadata.get("isPiAgentFinal") is not True
                or metadata.get("isAlphaDeckPipelineStep") is True
                or metadata.get("toolName") == "AlphaDeck 自动验证"
                or isinstance(metadata.get("validationStatus"), str)
                and metadata.get("reportPath") == ".data-agent/validation.json"
            ):
                continue
        content = js_slice(row["content"], 0, 2000).strip()
        if not content:
            continue
        if len(selected) >= 8 or characters + js_length(content) > 8000:
            break
        selected.append({"role": row["role"], "content": content})
        characters += js_length(content)
    return list(reversed(selected))


def modelport_headers():
    result = {}
    for variable, header, fallback in (
        ("ALPHADECK_MODELPORT_ORGANIZATION_ID", "X-ModelPort-Organization-Id", "org_local"),
        ("ALPHADECK_MODELPORT_PROJECT_ID", "X-ModelPort-Project-Id", "prj_alphadeck"),
        (
            "ALPHADECK_MODELPORT_ENVIRONMENT_ID",
            "X-ModelPort-Environment-Id",
            "env_production" if os.getenv("NODE_ENV") == "production" else "env_development",
        ),
    ):
        value = os.getenv(variable, "").strip() or fallback
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise ValueError(variable + " must contain 1-128 safe scope characters.")
        result[header] = value
    return result


class AgentExecutor:
    def __init__(self, projects, *, provider_factory=None, skill_root=ROOT, market_client=None):
        self.projects, self.database, self.provider_factory = (
            projects,
            projects.database,
            provider_factory,
        )
        self.skill_root, self.market_client = skill_root, market_client

    def provider(self, config):
        if self.provider_factory:
            return self.provider_factory(config)
        if not config["agent"]["enabled"]:
            raise AgentExecutionError(
                "MODEL_DISABLED", "项目 LLM Agent 已由 ALPHADECK_LLM_AGENT_ENABLED 禁用。"
            )
        key = os.getenv(config["credentialEnv"], "").strip()
        if not key:
            raise AgentExecutionError(
                "MODEL_UNCONFIGURED",
                config["credentialEnv"] + " 未配置，请在运行环境中注入模型凭据。",
            )
        return ChatProvider(
            key,
            config["baseUrl"],
            deepseek=config["provider"] == "deepseek",
            headers={
                "X-Client-App": "AlphaDeck-PI-Agent/python-1",
                **(modelport_headers() if config["provider"] == "openai" else {}),
            },
            limits={"maxRequestBytes": positive_env("PI_AGENT_MAX_REQUEST_BYTES", 2000000)},
            max_retries=positive_env("PI_AGENT_PROVIDER_MAX_RETRIES", 2, minimum=0),
            initial_delay_ms=positive_env("PI_AGENT_PROVIDER_RETRY_BASE_MS", 500, minimum=0),
            max_delay_ms=positive_env("PI_AGENT_PROVIDER_RETRY_MAX_MS", 10000, minimum=0),
        )

    async def execute(self, *args, **kwargs):
        deadline = asyncio.timeout(positive_env("PI_AGENT_TIMEOUT_MS", 1200000) / 1000)
        try:
            async with deadline:
                return await self._execute(*args, **kwargs)
        except Exception as cause:
            if deadline.expired():
                raise AgentExecutionError(
                    "EXECUTION_DEADLINE_EXCEEDED", "PI Agent 执行超时，任务已终止。"
                ) from cause
            raise

    async def _execute(
        self,
        project,
        instruction,
        request,
        *,
        model=None,
        profile="generation",
        images=(),
        personalization=None,
        knowledge=None,
        parent_request=None,
        cancel=None,
        observer=None,
    ):
        from .run_engine import RunEngine

        row = await self.projects.get(project)
        root = self.projects.managed_path(row, must_exist=True)
        requests = UserRequests(self.database)
        if not await requests.binding(project, request):
            raise AgentExecutionError(
                "REQUEST_NOT_FOUND", "Agent execution requires a project-bound request."
            )
        if await requests.cancelled(project, request):
            raise AgentExecutionError("RUN_CANCELLED", "用户暂停了当前任务")
        contract = execution_contract(
            root, instruction, profile=profile, has_attachments=bool(images)
        )
        tools = FinanceTools(root, market_client=self.market_client, **contract["toolOptions"])
        plan, graph, prepared = contract["plan"], contract["graph"], contract["assessment"]["ready"]
        deterministic = graph["providerMode"] == "deterministic"
        config = project_llm_config(model or row["selected_model"])
        template, variant = contract["templateId"], contract["variantId"]
        provider = (
            DeterministicProvider(
                [
                    {
                        "name": "apply_dashboard_spec",
                        "arguments": {"templateId": template, "variantId": variant},
                    },
                    {
                        "name": "submit_result",
                        "arguments": {
                            "summary": f"已通过可信渲染器生成 {template}/{variant} 标准量化看板。",
                            "artifacts": SOURCE_PATHS,
                            "notes": "零模型 Token 的确定性编译结果，等待平台独立验证。",
                        },
                    },
                ],
                name="pi-agent-trusted-renderer",
            )
            if deterministic
            else self.provider(config)
        )
        capability = FinanceCatalog().capability(contract["capabilityId"])
        skills = compile_skills(
            self.skill_root,
            runtime_workspace=root,
            capability_id=capability["id"],
            capability={
                "id": capability["id"],
                "status": capability["status"],
                "requiredSkillIds": capability["requiredSkills"],
            },
            required_skill_ids=contract["selectedSkillIds"],
            phase=contract["phase"],
            activated_skill_ids=["image-extraction", "data-quality"] if images else [],
            excluded_skill_ids=[
                *([] if images else ["image-extraction"]),
                *(["quant-symbol-resolver"] if (plan or {}).get("symbols") else []),
            ],
            template_id=template,
            variant_id=variant,
            available_tools=[value["name"] for value in tools.definitions],
            budget=positive_env(
                "PI_AGENT_PREFETCHED_SKILL_CONTEXT_CHARS"
                if prepared
                else "PI_AGENT_SKILL_CONTEXT_CHARS",
                4000 if prepared else 6000,
            ),
        )
        initial = None
        if prepared and contract["dashboardContractRequired"]:
            if deterministic:
                initial = js_json(
                    {
                        "schemaVersion": 1,
                        "mode": "trusted_dashboard_spec",
                        "templateId": template,
                        "variantId": variant,
                        "dataArtifact": "final_dashboard",
                        "digest": "sha256:"
                        + provenance_hash(
                            {
                                "runId": (plan or {}).get("runId"),
                                "templateId": template,
                                "variantId": variant,
                                "panels": (plan or {}).get("visualization", {}).get("panels"),
                            }
                        ),
                    }
                )
            else:
                inspection = await DashboardInspector(
                    WorkspacePolicy(root), max_output_chars=contract["toolOptions"]["max_output"]
                ).execute({})
                if inspection.get("ok") and inspection.get("content", "").strip():
                    initial = inspection["content"].strip()
        messages = [
            {
                "role": "system",
                "content": system_prompt(
                    phase=contract["phase"],
                    prepared_intent=contract["preparedIntent"],
                    skill_manifest=skills["systemContext"],
                ),
            },
            *await bounded_history(self.database, project, request),
            {
                "role": "user",
                "content": user_prompt(
                    task_packet=task_prompt(
                        instruction,
                        root,
                        run_plan=plan,
                        platform_prepared=prepared,
                        prepared_intent=contract["preparedIntent"],
                        phase=contract["phase"],
                        has_attachments=bool(images),
                    ),
                    skill_context=skills["taskContext"],
                    initial_dashboard_contract=initial,
                    require_dashboard_contract=contract["dashboardContractRequired"],
                    personalization_context=personalization.get("content")
                    if isinstance(personalization, dict)
                    else personalization,
                    governed_knowledge_context=knowledge.get("content")
                    if isinstance(knowledge, dict)
                    else knowledge,
                ),
            },
        ]
        budgets = graph["budgets"]
        output = min(
            positive_env("PI_AGENT_MAX_RUN_OUTPUT_TOKENS", budgets["maxOutputTokens"]),
            budgets["maxOutputTokens"],
        )
        per_turn = min(
            1 if deterministic else positive_env("PI_AGENT_MAX_TURN_OUTPUT_TOKENS", 12000), output
        )
        context = (
            None
            if deterministic
            else ContextManager(
                positive_env("PI_AGENT_CONTEXT_WINDOW_TOKENS", 128000),
                per_turn,
                min(
                    positive_env("PI_AGENT_MAX_INPUT_TOKENS", 48000),
                    budgets["maxPreparedInputTokens"],
                ),
                contextCapsuleMaxUtf8Bytes=min(
                    positive_env("PI_AGENT_CONTEXT_CAPSULE_MAX_BYTES", 2048), 2048
                ),
            )
        )
        run_id = "run-" + str(uuid4())
        async with resource_lock(
            root,
            {"purpose": "run_startup", "projectId": project, "requestId": request, "runId": run_id},
            recover_dead_local_owner=True,
        ):
            recovery = await reconcile_expired(self.database, project, root, already_locked=True)
            if recovery["blocked"] or recovery["racedRunIds"]:
                raise AgentExecutionError(
                    "RECONCILIATION_REQUIRED",
                    "PI Agent 检测到旧执行仍有未调和的写操作或并发调和，本次新执行已拒绝启动。",
                )
            provenance = {
                "id": run_id,
                "projectId": project,
                "requestId": request,
                "workspaceKey": workspace_identity(root),
                "provider": getattr(provider, "name", config["provider"]),
                "model": "pi-agent-deterministic-renderer-v1" if deterministic else config["model"],
                "frameworkVersion": build_identity()["frameworkVersion"],
                "buildRevision": build_identity()["buildRevision"],
                "profileHash": "sha256:"
                + provenance_hash(
                    {
                        "profile": profile,
                        "capabilityId": capability["id"],
                        "phase": contract["phase"],
                        "graph": graph,
                        "templateId": template,
                        "variantId": variant,
                    }
                ),
                "promptHash": "sha256:" + provenance_hash(messages),
                "toolHash": "sha256:" + provenance_hash(tools.definitions),
                "skillHash": "sha256:" + provenance_hash(skills["skills"]),
                "workspaceHash": "sha256:" + workspace_snapshot(root)["sha256"],
            }
            durable = await RuntimeStore(self.database).create(
                provenance,
                owner="agent-worker:" + str(uuid4()),
                lease_ttl_ms=positive_env("PI_AGENT_LEASE_TTL_MS", 60000),
            )
        async with RuntimeSession(
            self.database,
            durable,
            lease_ttl_ms=positive_env("PI_AGENT_LEASE_TTL_MS", 60000),
            heartbeat_seconds=positive_env("PI_AGENT_HEARTBEAT_INTERVAL_MS", 15000) / 1000,
        ) as session:
            writes = 0

            async def observe(event):
                nonlocal writes
                if (
                    event["type"] == "tool_completed"
                    and event.get("ok")
                    and event.get("toolName")
                    in {
                        definition["name"]
                        for definition in tools.definitions
                        if definition.get("effect") == "workspace_write"
                    }
                ):
                    writes += 1
                if observer:
                    await observer(event)

            engine = RunEngine(
                provider,
                provenance["model"],
                tools,
                max_turns=min(
                    positive_env("PI_AGENT_MAX_TURNS", budgets["maxTurns"]), budgets["maxTurns"]
                ),
                max_total_calls=min(
                    positive_env("PI_AGENT_MAX_TOTAL_TOOL_CALLS", budgets["maxToolCalls"]),
                    budgets["maxToolCalls"],
                ),
                max_output_tokens=output,
                max_tokens_per_turn=per_turn,
                require_write=True,
                max_input_tokens=positive_env("PI_AGENT_MAX_RUN_INPUT_TOKENS", 160000),
                max_cache_miss_tokens=min(
                    positive_env(
                        "PI_AGENT_MAX_RUN_CACHE_MISS_INPUT_TOKENS",
                        budgets["maxCacheMissInputTokens"],
                    ),
                    budgets["maxCacheMissInputTokens"],
                ),
                max_prepared_input_tokens=None
                if deterministic
                else min(
                    budgets["maxCumulativePreparedInputTokens"],
                    positive_env("PI_AGENT_MAX_RUN_INPUT_TOKENS", 160000),
                ),
                prewrite_reads=positive_env("PI_AGENT_PRE_WRITE_READ_ONLY_TURNS", 3),
                postwrite_reads=positive_env("PI_AGENT_POST_WRITE_READ_ONLY_TURNS", 2),
                context_manager=context,
                progress_stall_turns=budgets["progressStallTurns"],
                approval_handler=DurableApprovalHandler(self.database, project),
            )
            cancel = cancel or asyncio.Event()

            async def monitor():
                while True:
                    session.healthy()
                    if cancel.is_set() or await requests.cancelled(project, request):
                        cancel.set()
                        return
                    if parent_request and await requests.cancelled(project, parent_request):
                        cancel.set()
                        return
                    await asyncio.sleep(0.5)

            operation = asyncio.create_task(
                engine.run(
                    messages,
                    session=session,
                    cancel=cancel,
                    reasoning={
                        "enabled": not deterministic and env_flag("PI_AGENT_REASONING", True),
                        "effort": graph["reasoningEffort"],
                    },
                    observer=observe,
                )
            )
            monitoring = asyncio.create_task(monitor())
            try:
                done, _ = await asyncio.wait(
                    (operation, monitoring), return_when=asyncio.FIRST_COMPLETED
                )
                if monitoring in done:
                    await monitoring
                    raise AgentExecutionError("RUN_CANCELLED", "用户暂停了当前任务")
                result = await operation
            finally:
                for task in (operation, monitoring):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(operation, monitoring, return_exceptions=True)
            session.healthy()
        if result["status"] != "completed":
            raise AgentExecutionError(
                (result.get("error") or {}).get("code", "PI_AGENT_FAILED"),
                (result.get("error") or {}).get("message", "Agent 执行失败。"),
                repairable=writes > 0
                and result["status"] in {"max_turns", "max_tokens", "stopped"},
            )
        candidate = candidate_from_run(root, request, result)
        summary = (result["terminalResult"].get("data") or {}).get("summary")
        if summary:
            with suppress(Exception):
                async with self.database.transaction() as tx:
                    await insert_message(
                        tx,
                        project,
                        "assistant",
                        summary,
                        request_id=request,
                        metadata={
                            "runtime": "pi",
                            "isPiAgentCandidate": True,
                            "hidden_from_ui": True,
                        },
                    )
        return candidate
