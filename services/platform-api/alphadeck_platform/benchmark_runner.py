"""Native benchmark execution shares production planners, data contracts and validation."""

import asyncio
import base64
import json
import os
import re
import time
from copy import deepcopy
from uuid import uuid4

from .benchmark_artifacts import event_audit, inspect_artifacts
from .benchmark_visual import run_visual_check
from .config import ROOT
from .dashboard_spec import CATALOG as DASHBOARD_CATALOG
from .data_prefetch import prefetch_data
from .evaluation_diagnostics import trace_diagnostics
from .evaluation_evaluators import apply_evaluator
from .evaluation_scoring import deterministic_gate
from .finance_intent import clarification_continuation
from .finance_planning import write_initial_plan
from .generation_state import GenerationState, now
from .models import MODEL_IDS
from .prefetch_transport import MarketTransport, WorkspaceArtifacts
from .query_rewrite import rewrite_query
from .scaffold import ensure_scaffold
from .settings import DEFAULT_MODEL, normalize_model
from .validation_repair import repair_instruction, repair_plan, writable_globs
from .visualization import serialize_template
from .workspace import write_atomic

IMAGE = "iVBORw0KGgoAAAANSUhEUgAAAUAAAADwCAIAAABxLb1rAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAG0lEQVR4nO3BAQ0AAADCoPdPbQ43oAAAAAAAAAC4Gz+0AAFjTWbGAAAAAElFTkSuQmCC"


class BenchmarkRunner:
    def __init__(
        self,
        projects,
        previews,
        validator,
        *,
        root=ROOT,
        transport=None,
        runtime=None,
        provider=None,
    ):
        self.projects, self.previews, self.validator = projects, previews, validator
        self.root, self.transport, self.runtime, self.provider = (
            root,
            transport or MarketTransport(),
            runtime,
            provider,
        )
        self.fixtures = json.loads(
            (root / "benchmarks/alphadeck/query-rewrite-fixtures.json").read_text()
        )
        self.created = set()

    async def authority(self, operation):
        async with self.projects.database.transaction() as tx:
            identity, token = (
                os.getenv("ALPHADECK_EVAL_QUEUE_ID"),
                os.getenv("ALPHADECK_EVAL_LEASE_TOKEN"),
            )
            if (
                identity
                and not await (
                    await tx.execute(
                        "SELECT id FROM eval_queue_items WHERE id=%s AND lease_token=%s AND status='running' AND lease_expires_at>clock_timestamp() FOR UPDATE",
                        (identity, token),
                    )
                ).fetchone()
            ):
                raise RuntimeError("Benchmark 已失去评测队列租约。")
            return await operation(tx)

    async def rewrite(self, case, instruction, project, phase="primary"):
        fixture = self.fixtures.get("cases", {}).get(case["id"], {}).get(phase)
        if not fixture:
            raise ValueError(f"contract query rewrite fixture 缺失：{case['id']}/{phase}")

        async def semantics(_params):
            return {
                "ok": True,
                "data": deepcopy(fixture),
                "provider": self.fixtures["provider"],
                "model": self.fixtures["model"],
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
            }

        async def resolver(query, count):
            return await self.transport.fetch(
                "/api/v1/symbols/resolve", query={"query": query, "count": count}
            )

        return await rewrite_query(
            instruction,
            {"requestedCapabilityId": case.get("capabilityId"), "projectId": project},
            semantic_rewriter=semantics,
            resolver=resolver,
        )

    def basic(
        self,
        case,
        project,
        started,
        *,
        failures=None,
        artifacts=None,
        check_id="runtime_registry",
        summary="",
    ):
        failures = failures or []
        return {
            "id": case["id"],
            "name": case.get("name", case["id"]),
            "question": case.get("question", ""),
            "projectId": project,
            "projectPath": str(self.projects.root / project),
            "durationMs": round((time.monotonic() - started) * 1000),
            "passed": not failures,
            "failures": failures,
            "symbols": [],
            "prefetch": {"skipped": True, "summary": summary},
            "artifacts": artifacts or {},
            "validation": {
                "status": "failed" if failures else "passed",
                "checks": [
                    {
                        "id": check_id,
                        "status": "failed" if failures else "passed",
                        "summary": summary,
                    }
                ],
            },
        }

    def structural(self, case, project, started):
        kind, failures = case.get("type"), []
        if kind == "runtime_registry":
            if list(MODEL_IDS) != [
                DEFAULT_MODEL,
                "deepseek:deepseek-v4-flash",
                "deepseek-v4-flash",
            ]:
                failures.append("受控模型目录、顺序或默认模型漂移。")
            if (
                normalize_model("gpt-5.5") != DEFAULT_MODEL
                or normalize_model(MODEL_IDS[0]) != MODEL_IDS[0]
            ):
                failures.append("未注册模型应回退到默认模型；已注册模型必须保留。")
            return self.basic(
                case,
                project,
                started,
                failures=failures,
                artifacts={"registeredModels": list(MODEL_IDS), "defaultModel": DEFAULT_MODEL},
                summary="本地 Qwen、ModelPort DeepSeek 与官方直连接入边界检查。",
            )
        if kind == "renderer_capability_contract":
            matrix = [
                {key: item[key] for key in ("templateId", "variantId", "supported")}
                for item in DASHBOARD_CATALOG["capabilities"]
            ]
            expected = case.get("variantExpectations", [])
            if not expected or matrix != expected:
                failures.append("DashboardSpec renderer capability matrix 漂移。")
            selections = []
            for expectation in case.get("selectionExpectations", []):
                chosen = serialize_template(
                    expectation["capabilityId"],
                    {
                        "instruction": expectation["question"],
                        "symbolCount": expectation.get("symbolCount"),
                        "dataSignals": expectation.get("dataSignals"),
                    },
                )
                selections.append(
                    {
                        "capabilityId": expectation["capabilityId"],
                        "templateId": chosen["templateId"],
                        "variantId": chosen["variantId"],
                    }
                )
                if (
                    chosen["templateId"] != expectation["expectedTemplateId"]
                    or chosen["variantId"] != expectation["expectedVariantId"]
                ):
                    failures.append(expectation["capabilityId"] + " 可视化选择漂移。")
            return self.basic(
                case,
                project,
                started,
                failures=failures,
                artifacts={"capabilityMatrix": matrix, "selections": selections},
                check_id=kind,
                summary="DashboardSpec 变体覆盖、支持状态与选择路由检查。",
            )
        if kind == "repair_plan":
            report = {
                "schemaVersion": 1,
                "projectId": project,
                "reportPath": ".data-agent/validation.json",
                "status": "failed",
                "passed": False,
                "createdAt": now(),
                "updatedAt": now(),
                "checks": [
                    {"id": identity, "name": label, "status": "failed", "summary": message}
                    for identity, label, message in (
                        ("artifact_policy", "生成产物策略", "检测到外部脚本或 mock 数据。"),
                        ("chart_presence", "金融图表存在性", "缺少 K 线和成交量图表。"),
                        ("market_proxy", "/api/market 代理", "缺少同源代理。"),
                    )
                ],
            }
            repair, instruction = (
                repair_plan(report),
                repair_instruction(report, {"originalInstruction": case["question"]}),
            )
            if len(repair["steps"]) != 3 or writable_globs(report) != [
                "src/App.vue",
                "src/style.css",
            ]:
                failures.append("修复计划没有把可写范围限制到失败项对应文件。")
            if not all(
                marker in instruction
                for marker in (
                    "整个 `.data-agent/**`",
                    "结构修复和重新生成由平台负责",
                    "data_file/final/dashboard-data.json",
                    "/api/market",
                )
            ):
                failures.append("修复提示缺少平台与 Agent 的产物边界。")
            return self.basic(
                case,
                project,
                started,
                failures=failures,
                artifacts={"repairPlan": repair, "instructionPreview": instruction[:1000]},
                check_id=kind,
                summary="自动验证失败后的结构化修复计划检查。",
            )
        return None

    async def attachment(self, root, project, request, case, artifacts):
        if not case.get("imageAttachment"):
            return
        filename = case["imageAttachment"].get("filename") or "portfolio-screenshot.png"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.png", filename):
            raise ValueError("Invalid benchmark image filename")
        relative = "uploads/" + request + "/" + filename
        write_atomic(root, relative, base64.b64decode(IMAGE))
        await artifacts.write(
            ".data-agent/attachments.json",
            {
                "schemaVersion": 1,
                "projectId": project,
                "requestId": request,
                "createdAt": now(),
                "attachments": [
                    {
                        "id": "benchmark-image-1",
                        "name": filename,
                        "path": relative,
                        "mimeType": "image/png",
                        "size": len(base64.b64decode(IMAGE)),
                        "url": "/" + relative,
                        "publicUrl": "/" + relative,
                    }
                ],
            },
        )

    async def degradation(self, artifacts, plan, request):
        value = json.loads((ROOT / "config/benchmark-degradation-fixture.json").read_text())
        timestamp = now()

        def replace(item):
            if isinstance(item, dict):
                return {key: replace(child) for key, child in item.items()}
            if isinstance(item, list):
                return [replace(child) for child in item]
            if isinstance(item, str) and item.startswith("__VIS_") and item.endswith("__"):
                return plan["visualization"][item[6:-2]]
            return timestamp if item == "__NOW__" else request if item == "__REQUEST__" else item

        for relative, key in (
            ("data_file/final/dashboard-data.json", "finalData"),
            ("evidence/sources.json", "sources"),
            ("evidence/data_quality.json", "quality"),
        ):
            await artifacts.write(relative, replace(value[key]))
        for kind, stage in (
            ("data_prefetch_started", "data_collection"),
            ("data_prefetched", "data_collection"),
            ("data_quality_checked", "data_quality"),
        ):
            await artifacts.event(
                {
                    "event_type": kind,
                    "stage": stage,
                    "status": "warning",
                    "run_id": request,
                    "summary": "benchmark 使用含备用信源与缺口说明的受控降级证据。",
                }
            )
        return {
            "skipped": False,
            "summary": "使用合成降级证据验证 warning 级别链路。",
            "rawFiles": [],
            "symbols": ["600519"],
        }

    async def contract(self, case):
        started = time.monotonic()
        project = (
            "benchmark-" + re.sub(r"[^a-zA-Z0-9_-]", "-", case["id"])[:65] + "-" + uuid4().hex[:8]
        )
        structural = self.structural(case, project, started)
        if structural is not None:
            return structural
        await self.projects.create(
            {
                "projectId": project,
                "name": case.get("name") or case["id"],
                "initialPrompt": case["question"],
                "capabilityId": case.get("capabilityId"),
                "selectedModel": DEFAULT_MODEL,
            }
        )
        self.created.add(project)
        row = await self.projects.get(project)
        root = self.projects.managed_path(row, must_exist=True)
        request = "benchmark-" + uuid4().hex
        artifacts, state = (
            WorkspaceArtifacts(root, self.authority),
            GenerationState(root, authority=self.authority),
        )
        instruction = (
            "贵州茅台 600519 最近行情走势如何？"
            if case.get("type") == "source_degradation_contract"
            else case["question"]
        )
        await state.start(project, request, instruction, cli="benchmark", model=DEFAULT_MODEL)
        await self.attachment(root, project, request, case, artifacts)
        params = {
            "projectId": project,
            "requestId": request,
            "instruction": instruction,
            "capabilityId": case.get("capabilityId"),
            "capabilitySource": "benchmark",
            "hasImageAttachments": bool(case.get("imageAttachment")),
        }
        plan = await write_initial_plan(
            {**params, "queryRewrite": await self.rewrite(case, instruction, project)}, artifacts
        )
        prefetch = (
            await self.degradation(artifacts, plan, request)
            if case.get("type") == "source_degradation_contract"
            else await prefetch_data(plan, self.transport, artifacts)
        )
        failures = []
        if case.get("type") == "clarification_continuation":
            if plan["status"] != "needs_clarification" or not prefetch["skipped"]:
                failures.append("首轮应进入澄清且不预取数据。")
            continuation = clarification_continuation(
                {
                    "previousPlan": plan,
                    "instruction": case.get("followupAnswer", ""),
                    "capabilityId": case.get("capabilityId"),
                }
            )
            if continuation:
                request += "-followup"
                instruction = continuation["resolvedInstruction"]
                await state.start(
                    project, request, instruction, cli="benchmark", model=DEFAULT_MODEL
                )
                plan = await write_initial_plan(
                    {
                        **params,
                        "requestId": request,
                        "instruction": instruction,
                        "previousPlan": plan,
                        "queryRewrite": await self.rewrite(case, instruction, project, "followup"),
                    },
                    artifacts,
                )
                prefetch = await prefetch_data(plan, self.transport, artifacts)
            else:
                failures.append("用户补充信息后没有生成澄清承接上下文。")
        if case.get("expectClarification"):
            clarification = plan.get("clarification") or {}
            if (
                plan["status"] != "needs_clarification"
                or not clarification.get("required")
                or not prefetch["skipped"]
            ):
                failures.append("缺少关键信息时必须进入澄清，不得取数或生成页面。")
            for expected in case.get("expectedMissing", []):
                if expected not in clarification.get("missing", []):
                    failures.append("clarification.missing 应包含 " + expected)
            if (root / "data_file/final/dashboard-data.json").exists():
                failures.append("needs_clarification 时不应生成 final dashboard-data.json。")
            audit = event_audit(root, case, expect_final=False)
            failures.extend(audit["failures"])
            return {
                **self.basic(
                    case,
                    project,
                    started,
                    failures=failures,
                    artifacts={
                        "clarification": clarification,
                        "expectedMissing": case.get("expectedMissing", []),
                    },
                    check_id="intent_clarification",
                    summary="缺少关键信息时进入澄清流程，不取数不生成页面。",
                ),
                "symbols": plan["symbols"],
                "prefetch": prefetch,
                "eventAudit": audit,
            }
        await artifacts.mutate(lambda _tx: asyncio.to_thread(ensure_scaffold, root, project))
        validation = await self.validator.validate(project, artifacts, request)
        inspection = await inspect_artifacts(root, case, prefetch)
        visual_report = await run_visual_check(self, project, case)
        audit = event_audit(root, case)
        failures.extend(
            [
                *inspection["failures"],
                *(visual_report["failures"] if visual_report else []),
                *audit["failures"],
                *(
                    check["id"] + ": " + check["summary"]
                    for check in validation["checks"]
                    if check["status"] == "failed"
                ),
            ]
        )
        await state.update(
            project,
            request,
            "validation",
            "failed" if failures else "success",
            "benchmark 完整验收失败。" if failures else "benchmark 完整验收通过。",
            run_status="failed" if failures else "completed",
        )
        await self.previews.stop(project)
        return {
            "id": case["id"],
            "name": case.get("name", case["id"]),
            "question": case["question"],
            "projectId": project,
            "projectPath": str(root),
            "requestId": request,
            "durationMs": round((time.monotonic() - started) * 1000),
            "passed": not failures,
            "failures": failures,
            "symbols": plan["symbols"],
            "prefetch": prefetch,
            "artifacts": inspection,
            "eventAudit": audit,
            "visualCheck": visual_report,
            "repairAttempts": 0,
            "platformRepairCount": 0,
            "agentExecuted": False,
            "agentExecution": {"executed": False, "requestId": request},
            "validation": {"status": validation["status"], "checks": validation["checks"]},
            "executionMode": "contract",
        }

    async def run_case(self, case, options):
        started = time.monotonic()
        evaluation_mode = "e2e" if options["mode"] == "product-control" else options["mode"]
        try:
            value = (
                await self.contract(case)
                if options["mode"] == "contract"
                else await self.live(case, options)
            )
            review = None
            review_error = None
            if options["evaluatorId"] == "agent-review" and deterministic_gate(value)["passed"]:
                from .agent_reviewer import review_workspace

                try:
                    review = await review_workspace(
                        self.projects.root / value["projectId"],
                        case["question"],
                        case,
                        value,
                        model=options["model"],
                    )
                except Exception as error:
                    review_error = "语义审阅失败：" + str(error)
            evaluation = apply_evaluator(options["evaluatorId"], evaluation_mode, value, review)
            value.update(
                evaluation=evaluation,
                passed=evaluation["passed"],
                score=evaluation["score"],
                firstPassPassed=value["passed"]
                and evaluation["passed"]
                and not value.get("repairAttempts"),
                finalPassed=evaluation["passed"],
            )
            value["failures"] = list(
                dict.fromkeys(
                    [
                        *value["failures"],
                        *([review_error] if review_error else []),
                        *(
                            check["id"] + ": " + check["summary"]
                            for check in evaluation["checks"]
                            if check["status"] == "failed"
                        ),
                    ]
                )
            )
            value["traceDiagnostics"] = trace_diagnostics(value, evaluation_mode)
            return value
        except asyncio.CancelledError:
            raise
        except Exception as error:
            value = self.basic(
                case,
                "benchmark-failed-" + uuid4().hex,
                started,
                failures=[str(error)],
                check_id="benchmark_runtime",
                summary="benchmark 执行失败。",
            )
            evaluation = apply_evaluator(options["evaluatorId"], evaluation_mode, value)
            value.update(
                evaluation=evaluation,
                score=evaluation["score"],
                firstPassPassed=False,
                finalPassed=False,
                agentExecuted=False,
                repairAttempts=0,
            )
            value["traceDiagnostics"] = trace_diagnostics(value, evaluation_mode)
            return value

    async def live(self, case, options):
        from .benchmark_live import execute_live_case

        return await execute_live_case(self, case, options)
