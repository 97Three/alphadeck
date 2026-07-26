import asyncio
import hashlib
import json
import re
import time
from copy import deepcopy
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .context_capsule import ContextError
from .context_manager import estimate_tokens
from .file_tools import ToolContext
from .messages import js_length
from .progress import evaluate_turn, initial_state
from .provenance import js_json, provenance_hash
from .provider import ProviderError
from .tool_approval import apply_resolution, create_request, validate_policy
from .tool_arguments import parse_arguments
from .tool_paths import ToolError


class RunError(Exception):
    def __init__(self, code, message, status="failed"):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def operation_id(run, turn, call):
    return (
        "op_"
        + hashlib.sha256(
            (run + "\0" + str(turn) + "\0" + call["id"] + "\0" + call["name"]).encode()
        ).hexdigest()
    )


def safe_identifier(value, maximum):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and js_length(value) <= maximum
        and not re.search(r"[\x00-\x1f\x7f]", value)
    )


def serialize_tool_result(result):
    value = {"ok": bool(result["ok"])}
    key = "data" if result["ok"] else "error"
    if key in result:
        value[key] = result[key]
    if "content" in result:
        value["content"] = result["content"]
    try:
        return js_json(value)
    except (TypeError, ValueError, RecursionError):
        return js_json(
            {
                "ok": False,
                "error": {
                    "code": "SERIALIZATION_ERROR",
                    "message": "Tool result could not be serialized.",
                },
            }
        )


class RunEngine:
    def __init__(
        self,
        provider,
        model,
        tools,
        *,
        max_turns=48,
        max_output_tokens=12000,
        max_tokens_per_turn=None,
        max_total_calls=64,
        max_calls_per_turn=16,
        timeout_seconds=1200,
        require_terminal=True,
        require_write=False,
        max_input_tokens=None,
        max_cache_miss_tokens=None,
        max_prepared_input_tokens=None,
        prewrite_reads=6,
        postwrite_reads=3,
        context_manager=None,
        approval_handler=None,
        progress_stall_turns=2,
    ):
        self.provider, self.model, self.tools = provider, model, tools
        self.definitions = {definition["name"]: definition for definition in tools.definitions}
        if len(self.definitions) != len(tools.definitions):
            raise ValueError("Duplicate Agent tool names")
        self.max_turns, self.max_output_tokens = max_turns, max_output_tokens
        self.max_tokens_per_turn = max_tokens_per_turn or max_output_tokens
        self.max_total_calls, self.max_calls_per_turn = max_total_calls, max_calls_per_turn
        self.timeout_seconds, self.require_terminal, self.require_write = (
            timeout_seconds,
            require_terminal,
            require_write,
        )
        self.max_input_tokens, self.max_cache_miss_tokens, self.max_prepared_input_tokens = (
            max_input_tokens,
            max_cache_miss_tokens,
            max_prepared_input_tokens,
        )
        self.prewrite_reads, self.postwrite_reads = prewrite_reads, postwrite_reads
        self.context_manager = context_manager
        self.approval_handler = approval_handler
        self.progress_stall_turns = progress_stall_turns
        for definition in self.definitions.values():
            validate_policy(definition)
        if (
            any(definition.get("approval") for definition in self.definitions.values())
            and not approval_handler
        ):
            raise ValueError("Tools with approval policies require an approval handler")
        if any(
            type(value) is not int or value <= 0
            for value in (
                max_turns,
                max_output_tokens,
                self.max_tokens_per_turn,
                max_total_calls,
                max_calls_per_turn,
                prewrite_reads,
                postwrite_reads,
                progress_stall_turns,
            )
        ):
            raise ValueError("Agent limits must be positive integers")

    async def run(
        self, messages, *, run_id=None, session=None, cancel=None, reasoning=None, observer=None
    ):
        run_id = run_id or (session.row["id"] if session else str(uuid4()))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", run_id):
            raise ValueError("Invalid Agent run ID")
        state = {
            "messages": deepcopy(messages),
            "turns": 0,
            "usage": {
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
                "cachedInputTokens": 0,
                "cacheMissInputTokens": 0,
            },
            "output": "",
            "totalCalls": 0,
            "writes": 0,
            "readTurns": 0,
            "oracle": initial_state(),
            "preparedTokens": 0,
            "terminal": None,
            "capsule": self.context_manager.create_capsule_session()
            if self.context_manager
            else None,
        }
        started = int(time.time() * 1000)

        async def emit(kind, payload, **options):
            if session:
                await session.event(kind, payload, **options)
            if observer:
                try:
                    async with asyncio.timeout(5):
                        await observer({"type": kind, "runId": run_id, **payload})
                except Exception:
                    pass

        async def checkpoint(boundary):
            if session:
                await session.checkpoint(
                    state["turns"],
                    boundary,
                    {
                        "schemaVersion": 1,
                        "stage": boundary,
                        "turn": state["turns"],
                        "toolCalls": state["totalCalls"],
                        "workspaceWrites": state["writes"],
                        "progressOracle": state["oracle"],
                    },
                )

        status, error = "failed", None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await emit(
                    "run_started", {"provider": type(self.provider).__name__, "model": self.model}
                )
                await checkpoint("run_started")
                await self._loop(run_id, state, session, cancel, reasoning, emit, checkpoint)
                status = "completed"
        except RunError as cause:
            status, error = cause.status, {"code": cause.code, "message": cause.message}
        except ProviderError as cause:
            status, error = (
                "failed",
                {"code": "PROVIDER_" + cause.code, "message": "模型调用失败。"},
            )
        except ContextError as cause:
            status, error = "failed", {"code": cause.code, "message": cause.message}
        except TimeoutError:
            status, error = (
                "timed_out",
                {"code": "RUN_TIMEOUT", "message": "Agent exceeded its execution deadline."},
            )
        except asyncio.CancelledError:
            status, error = (
                "cancelled",
                {"code": "RUN_CANCELLED", "message": "Agent execution was cancelled."},
            )
        except Exception as cause:
            if session and session.fatal:
                raise
            status, error = (
                "failed",
                {
                    "code": "RUN_EXECUTION_FAILED",
                    "message": "Agent execution failed before candidate submission.",
                },
            )
            # Preserve the private cause for callers/logging without persisting it in runtime records.
            state["privateCause"] = cause
        result = {
            "runId": run_id,
            "status": status,
            "messages": state["messages"],
            "output": state["output"],
            "turns": state["turns"],
            "usage": state["usage"],
            "startedAt": started,
            "finishedAt": int(time.time() * 1000),
            **({"error": error} if error else {}),
        }
        if state["terminal"]:
            result.update(
                terminalToolCall=state["terminal"][0], terminalResult=state["terminal"][1]
            )
        if session:
            await session.event(
                "run_completed",
                {
                    "status": status,
                    "turns": state["turns"],
                    **({"errorCode": error["code"]} if error else {}),
                },
                usage=state["usage"],
            )
            durable = (
                "candidate_complete"
                if state["terminal"]
                else "completed"
                if status == "completed"
                else status
                if status in {"failed", "cancelled", "timed_out", "interrupted"}
                else "failed"
            )
            await session.complete(durable, state["turns"], state["usage"], error)
        return result

    async def _loop(self, run_id, state, session, cancel, reasoning, emit, checkpoint):
        for turn in range(1, self.max_turns + 1):
            if cancel and cancel.is_set():
                raise RunError("RUN_CANCELLED", "Agent execution was cancelled.", "cancelled")
            if session:
                session.healthy()
            usage = state["usage"]
            for limit, actual, code in (
                (self.max_input_tokens, usage["inputTokens"], "MAX_RUN_INPUT_TOKENS"),
                (
                    self.max_cache_miss_tokens,
                    usage["cacheMissInputTokens"],
                    "MAX_RUN_CACHE_MISS_INPUT_TOKENS",
                ),
            ):
                if limit is not None and actual >= limit:
                    raise RunError(
                        code, "Agent exhausted its cumulative input budget.", "max_tokens"
                    )
            if usage["outputTokens"] >= self.max_output_tokens:
                raise RunError("MAX_TOKENS", "Agent exhausted its output budget.", "max_tokens")
            state["turns"] = turn
            threshold = self.postwrite_reads if state["writes"] else self.prewrite_reads
            definitions = [
                definition
                for definition in self.definitions.values()
                if not (state["readTurns"] >= threshold and definition["effect"] == "read")
            ]
            # Application callbacks belong to the host, never the model's JSON tool catalog.
            public_definitions = [
                {key: definition[key] for key in ("name", "description", "inputSchema")}
                for definition in definitions
            ]
            messages = state["messages"]
            if self.context_manager:
                capsule = state["capsule"].checkpoint(
                    "writing" if state["writes"] else "exploration"
                )
                context = self.context_manager.prepare(
                    messages, public_definitions, {"contextCapsule": capsule}
                )
                state["messages"] = context["messages"]
                messages = context["messages"] + context["requestLocalMessages"]
                prepared = context["estimate"]["preparedInputTokens"]
                if context["compaction"]["applied"]:
                    await emit(
                        "context_compacted",
                        {
                            "turn": turn,
                            "estimate": context["estimate"],
                            "compaction": context["compaction"],
                        },
                    )
            else:
                prepared = estimate_tokens(messages, public_definitions)
            state["preparedTokens"] += prepared
            if (
                self.max_prepared_input_tokens is not None
                and state["preparedTokens"] > self.max_prepared_input_tokens
            ):
                raise RunError(
                    "MAX_RUN_PREPARED_INPUT_TOKENS",
                    "Agent exhausted its prepared-input budget.",
                    "max_tokens",
                )
            await emit("model_turn_started", {"turn": turn, "preparedInputTokens": prepared})
            request = {
                "model": self.model,
                "messages": messages,
                "tools": public_definitions,
                "maxTokens": min(
                    self.max_tokens_per_turn, self.max_output_tokens - usage["outputTokens"]
                ),
                **({"reasoning": reasoning} if reasoning is not None else {}),
            }
            if not (reasoning and reasoning.get("enabled")) and definitions:
                request["toolChoice"] = "auto"
            text, private_reasoning = "", ""
            calls = {}
            turn_usage = None
            finish = None
            async for event in self.provider.complete(request):
                if cancel and cancel.is_set():
                    raise RunError("RUN_CANCELLED", "Agent execution was cancelled.", "cancelled")
                if session:
                    session.healthy()
                if event["type"] == "text_delta":
                    text += event["delta"]
                    state["output"] += event["delta"]
                elif event["type"] == "reasoning_delta":
                    private_reasoning += event["delta"]
                elif event["type"] == "usage":
                    turn_usage = event["usage"]
                elif event["type"] == "finish":
                    finish = event["reason"]
                elif event["type"] == "tool_call_delta":
                    call = calls.setdefault(event["index"], {"id": "", "name": "", "arguments": ""})
                    if "id" in event:
                        if call["id"] and call["id"] != event["id"]:
                            raise RunError(
                                "INVALID_TOOL_CALL_ID", "Model changed a tool-call identity."
                            )
                        call["id"] = event["id"]
                    call["name"] += event.get("nameDelta", "")
                    call["arguments"] += event.get("argumentsDelta", "")
                elif event["type"] == "provider_retry":
                    await emit(
                        "provider_retry",
                        {key: value for key, value in event.items() if key != "type"},
                    )
            ordered = [calls[index] for index in sorted(calls)]
            if turn_usage is None:
                output = (
                    len(text.encode())
                    + len(private_reasoning.encode())
                    + len(json.dumps(ordered, ensure_ascii=False, separators=(",", ":")).encode())
                    if ordered
                    else len(text.encode()) + len(private_reasoning.encode())
                )
                turn_usage = {
                    "inputTokens": prepared,
                    "outputTokens": output,
                    "totalTokens": prepared + output,
                    "cachedInputTokens": 0,
                    "cacheMissInputTokens": prepared,
                    "usageSource": "estimated",
                }
            for key in (
                "inputTokens",
                "outputTokens",
                "totalTokens",
                "cachedInputTokens",
                "cacheMissInputTokens",
                "reasoningTokens",
            ):
                if key in turn_usage:
                    usage[key] = usage.get(key, 0) + turn_usage[key]
            await emit(
                "model_turn_completed",
                {
                    "turn": turn,
                    "textCharacters": js_length(text),
                    "reasoningCharacters": js_length(private_reasoning),
                    "toolCallCount": len(ordered),
                },
                usage=usage,
            )
            await checkpoint("model_turn_completed")
            if finish == "length" or usage["outputTokens"] >= self.max_output_tokens:
                raise RunError(
                    "MAX_TOKENS", "Provider stopped at its output-token limit.", "max_tokens"
                )
            if finish in {"content_filter", "resource_exhausted"}:
                raise RunError(
                    "PROVIDER_" + finish.upper(),
                    "Provider stopped before completing the requested turn.",
                )
            if (
                ordered
                and any(self.definitions.get(call["name"], {}).get("terminal") for call in ordered)
                and len(ordered) != 1
            ):
                raise RunError(
                    "TERMINAL_TOOL_NOT_EXCLUSIVE",
                    "A terminal tool must be the only tool call in its turn.",
                )
            if (
                len(ordered) > self.max_calls_per_turn
                or state["totalCalls"] + len(ordered) > self.max_total_calls
            ):
                raise RunError(
                    "MAX_TOOL_CALLS_PER_TURN"
                    if len(ordered) > self.max_calls_per_turn
                    else "MAX_TOTAL_TOOL_CALLS",
                    "Agent exceeded its tool-call budget.",
                )
            if len({call["id"] for call in ordered}) != len(ordered):
                raise RunError(
                    "DUPLICATE_TOOL_CALL_ID", "Model repeated a tool-call ID within one turn."
                )
            if any(
                not safe_identifier(call["id"], 512) or not safe_identifier(call["name"], 256)
                for call in ordered
            ):
                raise RunError(
                    "INVALID_TOOL_CALL_ID", "Model returned an invalid tool-call name or identity."
                )
            state["messages"].append(
                {
                    "role": "assistant",
                    "content": text or None,
                    **({"reasoningContent": private_reasoning} if private_reasoning else {}),
                    **({"toolCalls": ordered} if ordered else {}),
                }
            )
            if not ordered:
                if self.require_terminal:
                    raise RunError(
                        "TERMINAL_TOOL_REQUIRED",
                        "Agent stopped without successfully submitting a candidate.",
                        "stopped",
                    )
                return
            observations = []
            effects = []
            writes = 0
            for call in ordered:
                definition = self.definitions.get(call["name"])
                effect = definition["effect"] if definition else "external_write"
                effects.append(effect)
                state["totalCalls"] += 1
                operation = operation_id(run_id, turn, call)
                prepared_operation = False
                arguments = {}
                try:
                    if not definition or definition not in definitions:
                        raise ToolError(
                            "UNKNOWN_TOOL", "Tool is unavailable in this execution phase."
                        )
                    arguments = parse_arguments(call["arguments"])["value"]
                    Draft202012Validator(definition["inputSchema"]).validate(arguments)
                    if self.require_write and definition.get("terminal") and state["writes"] == 0:
                        raise RunError(
                            "WORKSPACE_WRITE_REQUIRED",
                            "Agent must complete a governed workspace write before submit_result.",
                        )
                    if effect in {"workspace_write", "external_write"} and not session:
                        raise ToolError(
                            "WORKSPACE_COMMIT_FENCE_REQUIRED",
                            "Mutating tools require a durable Agent session.",
                        )
                    if definition.get("approval"):
                        request = create_request(
                            run_id, turn, call, definition, arguments, int(time.time() * 1000)
                        )
                        await emit(
                            "tool_approval_requested",
                            {"turn": turn, "request": request},
                            transition="waiting",
                        )
                        await checkpoint("waiting_for_external_input")
                        try:
                            async with asyncio.timeout(
                                max(0.001, (request["expiresAt"] - int(time.time() * 1000)) / 1000)
                            ):
                                resolution = await self.approval_handler(request, cancel=cancel)
                        except TimeoutError:
                            resolution = {"decision": "reject", "resolvedBy": "expired"}
                        effective, effective_hash = apply_resolution(
                            request, resolution, definition, call
                        )
                        arguments = parse_arguments(effective["arguments"])["value"]
                        Draft202012Validator(definition["inputSchema"]).validate(arguments)
                        await emit(
                            "tool_approval_resolved",
                            {
                                "turn": turn,
                                "approvalId": request["approvalId"],
                                "toolCallId": call["id"],
                                "toolName": call["name"],
                                "decision": resolution["decision"],
                                "inputSha256": request["inputSha256"],
                                "effectiveInputSha256": effective_hash,
                                **(
                                    {"resolvedBy": resolution["resolvedBy"]}
                                    if resolution.get("resolvedBy")
                                    else {}
                                ),
                            },
                            transition="running",
                        )
                        if resolution["decision"] == "reject":
                            raise ToolError(
                                "TOOL_APPROVAL_REJECTED",
                                "Tool execution was rejected by its approval policy.",
                            )
                    input_hash = provenance_hash(arguments)
                    if session:
                        await session.prepare(operation, call, definition, input_hash)
                        prepared_operation = True
                    await emit(
                        "tool_started",
                        {
                            "turn": turn,
                            "operationId": operation,
                            "toolName": call["name"],
                            "effect": effect,
                            "inputSha256": input_hash,
                        },
                    )

                    async def authorize(commit, identity=operation):
                        return await session.authorize(identity, commit)

                    context = ToolContext(run_id, operation, authorize if session else None)
                    async with asyncio.timeout(15):
                        result = await self.tools.execute(call["name"], arguments, context)
                except (ToolError, ValidationError, ValueError) as cause:
                    if (
                        definition
                        and definition.get("approval")
                        and session
                        and session.row["status"] == "waiting"
                    ):
                        raise RunError(
                            "INVALID_TOOL_APPROVAL_RESOLUTION",
                            "Approval or edited tool input failed validation.",
                        ) from cause
                    code = cause.code if isinstance(cause, ToolError) else "INVALID_TOOL_ARGUMENTS"
                    message = (
                        cause.message
                        if isinstance(cause, ToolError)
                        else "Tool input failed its JSON contract."
                    )
                    result = {
                        "ok": False,
                        "error": {"code": code, "message": message},
                        "content": message,
                    }
                except TimeoutError:
                    result = {
                        "ok": False,
                        "error": {
                            "code": "TOOL_TIMEOUT",
                            "message": "Tool execution exceeded its deadline.",
                        },
                        "content": "Tool execution timed out.",
                    }
                # RunError and storage failures escape: a stale worker cannot report a successful tool.
                uncertain = (
                    not result["ok"]
                    and effect in {"workspace_write", "external_write"}
                    and result["error"]["code"] not in PRE_EXECUTION_ERRORS
                )
                if session and prepared_operation:
                    await session.tool_finished(
                        operation,
                        result,
                        uncertain=uncertain,
                        workspace=self.tools.policy.root if hasattr(self.tools, "policy") else None,
                    )
                await emit(
                    "tool_completed",
                    {
                        "turn": turn,
                        "operationId": operation,
                        "toolName": call["name"],
                        "ok": result["ok"],
                        "resultSha256": provenance_hash(result),
                        **({"errorCode": result["error"]["code"]} if not result["ok"] else {}),
                    },
                )
                state["messages"].append(
                    {
                        "role": "tool",
                        "toolCallId": call["id"],
                        "name": call["name"],
                        "content": serialize_tool_result(result),
                    }
                )
                if state["capsule"] and definition:
                    operation_fact = {
                        "operationId": operation,
                        "toolCallId": call["id"],
                        "toolName": call["name"],
                        "turn": turn,
                        "effect": effect,
                        "terminal": bool(definition.get("terminal")),
                        "resultSha256": provenance_hash(result),
                    }
                    projector = getattr(self.tools, "project_context_receipt", None)
                    receipt = projector(call["name"], arguments, result) if projector else None
                    if receipt is not None:
                        projected = (
                            {
                                key: receipt[key]
                                for key in ("targetReferences", "artifactSha256", "bytes")
                                if key in receipt
                            }
                            if isinstance(receipt, dict)
                            else {"targetReferences": receipt}
                        )
                        state["capsule"].record({**operation_fact, "result": result, **projected})
                    else:
                        state["capsule"].record_framework_outcome(
                            {
                                **operation_fact,
                                "status": "succeeded" if result["ok"] else "failed",
                                "targetIdentitySha256": provenance_hash(call["arguments"]),
                            }
                        )
                observations.append(
                    provenance_hash(
                        {"tool": call["name"], "input": call["arguments"], "result": result}
                    )
                )
                if uncertain:
                    raise RunError(
                        "WORKSPACE_RECONCILIATION_REQUIRED",
                        "A mutating tool has an uncertain outcome and cannot be replayed.",
                    )
                if result["ok"] and effect == "workspace_write":
                    writes += 1
                    state["writes"] += 1
                if result["ok"] and definition and definition.get("terminal"):
                    state["terminal"] = (call, result)
                    return
            state["readTurns"] = (
                state["readTurns"] + 1
                if effects and all(effect == "read" for effect in effects)
                else 0
            )
            progress = evaluate_turn(
                state["oracle"],
                {
                    "trustedFactFingerprints": observations,
                    "workspaceFingerprint": provenance_hash(
                        {"writes": state["writes"], "observations": observations}
                    ),
                    "toolObservationFingerprints": observations,
                    "successfulWorkspaceWrites": writes,
                },
                self.progress_stall_turns,
            )
            state["oracle"] = progress["state"]
            await emit(
                "progress_evaluated",
                {
                    "turn": turn,
                    "progressOracle": state["oracle"],
                    "decision": {
                        key: progress["decision"][key]
                        for key in (
                            "progressed",
                            "stalled",
                            "consecutiveNoProgressTurns",
                            "progressSignals",
                            "stallSignals",
                        )
                    },
                },
            )
            await checkpoint("tools_completed")
        raise RunError("MAX_TURNS", "Agent exhausted its turn budget.", "max_turns")


PRE_EXECUTION_ERRORS = {
    "UNKNOWN_TOOL",
    "INVALID_TOOL_ARGUMENTS",
    "INVALID_TOOL_INPUT",
    "TOOL_APPROVAL_REJECTED",
    "ABSOLUTE_PATH_DENIED",
    "BINARY_FILE_DENIED",
    "DUPLICATE_BATCH_TARGET",
    "EDIT_MATCH_AMBIGUOUS",
    "EDIT_MATCH_NOT_FOUND",
    "EXECUTABLE_CONFIG_DENIED",
    "FILE_TOO_LARGE",
    "INVALID_PATH",
    "INVALID_BATCH_WRITE",
    "INVALID_BATCH_WRITE_LIMIT",
    "INVALID_WORKSPACE",
    "NOT_A_FILE",
    "PATH_NOT_FOUND",
    "PATH_RESOLUTION_FAILED",
    "PATH_TRAVERSAL_DENIED",
    "SENSITIVE_PATH_DENIED",
    "PLATFORM_PATH_READ_ONLY",
    "SYMLINK_ESCAPE_DENIED",
    "SYMLINK_WRITE_DENIED",
    "WORKSPACE_COMMIT_FENCE_REQUIRED",
    "WORKSPACE_JOURNAL_PREPARE_FAILED",
    "WORKSPACE_MUTATION_ROLLED_BACK",
    "WORKSPACE_WRITE_CONFLICT",
    "WRITE_PATH_DENIED",
    "WRITE_TOO_LARGE",
}
