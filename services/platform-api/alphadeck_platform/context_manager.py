"""Protocol-preserving context compaction with original TypeScript-compatible telemetry."""

import hashlib
import json
import math
from copy import deepcopy

from .context_capsule import (
    PREFIX,
    CapsuleSession,
    ContextError,
    assert_capsule,
    safe_integer,
    utf8,
)
from .provenance import js_json


def clone_message(message):
    role = message["role"]
    value = {"role": role, "content": message["content"]}
    keys = {"assistant": ("reasoningContent", "toolCalls"), "tool": ("toolCallId", "name")}.get(
        role, ()
    )
    for key in keys:
        if key in message:
            value[key] = deepcopy(message[key])
    return value


def estimate_tokens(messages, tools=()):
    serialized = js_json({"messages": messages, "tools": list(tools)})
    estimate, run = 0, 0
    for character in serialized + "\0":
        if character.isascii() and (character.isalnum() or character == "_"):
            run += 1
            continue
        if run:
            estimate += math.ceil(run * 0.75) if run >= 32 else math.ceil(run / 3)
            run = 0
        if character != "\0":
            estimate += (
                (0.25 if character.isspace() else 0.5)
                if character.isascii()
                else len(utf8(character)) / 2
            )
    calls = sum(
        len(message.get("toolCalls", [])) for message in messages if message["role"] == "assistant"
    )
    return math.ceil((estimate + 64 + len(messages) * 8 + len(tools) * 12 + calls * 12) * 1.1)


def build_groups(messages):
    groups, index = [], 0

    def invalid(reason, details):
        raise ContextError(
            "INVALID_CONTEXT_HISTORY",
            "Tool-call history must contain complete adjacent call/result clusters.",
            {**details, "reason": reason},
        )

    while index < len(messages):
        message = messages[index]
        if message["role"] == "tool":
            invalid(
                "orphan_tool_result", {"messageIndex": index, "toolCallId": message["toolCallId"]}
            )
        entries = [{"index": index, "message": clone_message(message)}]
        cluster = message["role"] == "assistant" and bool(message.get("toolCalls"))
        if cluster:
            ids, results = set(), set()
            for call in message["toolCalls"]:
                if not call["id"] or call["id"] in ids:
                    invalid(
                        "invalid_tool_call_id", {"messageIndex": index, "toolCallId": call["id"]}
                    )
                ids.add(call["id"])
            while index + 1 < len(messages) and messages[index + 1]["role"] == "tool":
                index += 1
                result = messages[index]
                call = result["toolCallId"]
                if call not in ids or call in results:
                    invalid(
                        "duplicate_tool_result" if call in ids else "unknown_tool_call_id",
                        {"messageIndex": index, "toolCallId": call},
                    )
                results.add(call)
                entries.append({"index": index, "message": clone_message(result)})
            if ids - results:
                missing = [call["id"] for call in message["toolCalls"] if call["id"] not in results]
                invalid(
                    "missing_tool_results",
                    {"messageIndex": entries[0]["index"], "missingToolCallIds": missing},
                )
        groups.append(
            {
                "kind": "tool_call_cluster" if cluster else "message",
                "entries": entries,
                "protected": False,
            }
        )
        index += 1
    return groups


def flatten(groups):
    return [entry["message"] for group in groups for entry in group["entries"]]


def group_metadata(group):
    return {
        "kind": group["kind"],
        "messageIndexes": [entry["index"] for entry in group["entries"]],
        "roles": [entry["message"]["role"] for entry in group["entries"]],
    }


def is_summary(content):
    try:
        marker = json.loads(content)["$piAgent"]
        return (
            marker["kind"] == "tool_result_truncation"
            and marker["version"] == 1
            and marker["generatedBy"] == "PiAgentContextManager"
        )
    except (ValueError, KeyError, TypeError):
        return False


def summarize(message):
    raw = utf8(message["content"])
    preview = raw[:512].decode("utf-8", "ignore") if len(raw) > 512 else message["content"]
    checksum = hashlib.sha256(raw).hexdigest()
    payload = {
        "$piAgent": {
            "kind": "tool_result_truncation",
            "version": 1,
            "generatedBy": "PiAgentContextManager",
            "toolCallId": message["toolCallId"],
            **({"toolName": message["name"]} if "name" in message else {}),
            "digest": {"algorithm": "SHA-256", "hex": checksum},
            "originalUtf8Bytes": len(raw),
            "retainedPreviewUtf8Bytes": len(utf8(preview)),
            "truncated": True,
            "previewTrust": "untrusted_tool_output",
            "preview": preview,
        }
    }
    content = js_json(payload)
    return {**message, "content": content}, {
        "toolCallId": message["toolCallId"],
        "sha256": checksum,
        "originalUtf8Bytes": len(raw),
        "summaryUtf8Bytes": len(utf8(content)),
        "retainedPreviewUtf8Bytes": len(utf8(preview)),
    }


class ContextManager:
    def __init__(
        self,
        contextWindowTokens,
        reservedOutputTokens,
        maxInputTokens,
        *,
        tokenEstimator=None,
        contextCapsuleMaxUtf8Bytes=None,
    ):
        self.options = {
            "contextWindowTokens": contextWindowTokens,
            "reservedOutputTokens": reservedOutputTokens,
            "maxInputTokens": maxInputTokens,
        }
        for key, value in self.options.items():
            minimum = 0 if key == "reservedOutputTokens" else 1
            if not safe_integer(value, minimum):
                raise ContextError(
                    "INVALID_CONTEXT_CONFIGURATION",
                    f"{key} must be a safe integer greater than or equal to {minimum}",
                    {"field": key, "value": value, "minimum": minimum},
                )
        if reservedOutputTokens >= contextWindowTokens:
            raise ContextError(
                "INVALID_CONTEXT_CONFIGURATION",
                "reservedOutputTokens must be smaller than contextWindowTokens",
                {
                    "contextWindowTokens": contextWindowTokens,
                    "reservedOutputTokens": reservedOutputTokens,
                },
            )
        if contextCapsuleMaxUtf8Bytes is not None and not safe_integer(
            contextCapsuleMaxUtf8Bytes, 256
        ):
            raise ContextError(
                "INVALID_CONTEXT_CONFIGURATION",
                "contextCapsuleMaxUtf8Bytes must be a safe integer greater than or equal to 256",
                {
                    "field": "contextCapsuleMaxUtf8Bytes",
                    "value": contextCapsuleMaxUtf8Bytes,
                    "minimum": 256,
                },
            )
        self.maximum_capsule = contextCapsuleMaxUtf8Bytes or 8192
        self.estimator = tokenEstimator or estimate_tokens
        self.budget = min(maxInputTokens, contextWindowTokens - reservedOutputTokens)

    def create_capsule_session(self):
        return CapsuleSession(self.maximum_capsule)

    def estimate(self, messages, tools):
        try:
            value = self.estimator(messages, tools)
        except Exception as cause:
            raise ContextError(
                "TOKEN_ESTIMATION_FAILED",
                "PI Agent context token estimator failed",
                {"cause": str(cause)},
            ) from cause
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ContextError(
                "TOKEN_ESTIMATION_FAILED",
                "PI Agent context token estimator must return a finite non-negative number",
                {"estimate": value},
            )
        return math.ceil(value)

    def prepare(self, messages, tools=(), preparation=None):
        preparation = preparation or {}
        budget = preparation.get("inputBudgetTokens", self.budget)
        if not safe_integer(budget, 1):
            raise ContextError(
                "INVALID_CONTEXT_CONFIGURATION",
                "inputBudgetTokens must be a positive safe integer when provided",
                {"field": "inputBudgetTokens", "value": budget},
            )
        budget = min(self.budget, budget)
        groups = build_groups(messages)
        local = [clone_message(message) for message in preparation.get("requestLocalMessages", [])]
        capsule_telemetry, dropped = None, []
        capsule = preparation.get("contextCapsule")
        if capsule:
            assert_capsule(capsule)
            active = next(
                (group for group in reversed(groups) if group["kind"] == "tool_call_cluster"), None
            )
            covered = set(capsule["coveredToolCallIds"])
            previous, replaced, retained = [], [], []
            for group in groups:
                message = group["entries"][0]["message"]
                if (
                    group["kind"] == "message"
                    and message["role"] == "system"
                    and message["content"].startswith(PREFIX)
                ):
                    previous.append(group)
                elif (
                    group is not active
                    and group["kind"] == "tool_call_cluster"
                    and all(call["id"] in covered for call in message["toolCalls"])
                ):
                    replaced.append(group)
                else:
                    retained.append(group)
            groups = retained
            dropped.extend(group_metadata(group) for group in replaced)
            if preparation.get("emitContextCapsuleRequestLocalMessage") is not False:
                local.append({"role": "user", "content": capsule["content"]})
            capsule_telemetry = {
                "applied": True,
                **capsule["telemetry"],
                "replacedToolCallClusters": len(replaced),
                "replacedMessages": sum(len(group["entries"]) for group in previous + replaced),
                "replacedPreviousCapsule": bool(previous),
            }
        original = self.estimate([clone_message(message) for message in messages] + local, tools)
        entries = [entry for group in groups for entry in group["entries"]]
        systems = [entry["index"] for entry in entries if entry["message"]["role"] == "system"]
        latest_user = next(
            (entry["index"] for entry in reversed(entries) if entry["message"]["role"] == "user"),
            None,
        )
        active = next(
            (group for group in reversed(groups) if group["kind"] == "tool_call_cluster"), None
        )
        for group in groups:
            group["protected"] = group is active or any(
                entry["message"]["role"] == "system" or entry["index"] == latest_user
                for entry in group["entries"]
            )
        active_indices = [entry["index"] for entry in active["entries"]] if active else []
        removed, summaries = [], []

        def estimate(candidate=None):
            return self.estimate(flatten(groups if candidate is None else candidate) + local, tools)

        prepared = estimate()
        for group in groups:
            if prepared <= budget:
                break
            if group["kind"] == "tool_call_cluster":
                continue
            for entry in group["entries"]:
                message = entry["message"]
                if (
                    prepared > budget
                    and message["role"] == "assistant"
                    and "reasoningContent" in message
                ):
                    removed.append(
                        {
                            "messageIndex": entry["index"],
                            "originalUtf8Bytes": len(utf8(message.pop("reasoningContent"))),
                        }
                    )
                    prepared = estimate()

        def summarize_entry(entry):
            nonlocal prepared
            original_message = entry["message"]
            summary, metadata = summarize(original_message)
            entry["message"] = summary
            candidate = estimate()
            if candidate < prepared:
                prepared = candidate
                summaries.append({"messageIndex": entry["index"], **metadata})
            else:
                entry["message"] = original_message

        for group in groups:
            if prepared <= budget:
                break
            if group["protected"] or group["kind"] != "tool_call_cluster":
                continue
            for entry in group["entries"]:
                if (
                    prepared > budget
                    and entry["message"]["role"] == "tool"
                    and not is_summary(entry["message"]["content"])
                ):
                    summarize_entry(entry)
        if prepared > budget:
            retained = []
            for index, group in enumerate(groups):
                if group["protected"]:
                    retained.append(group)
                    continue
                dropped.append(group_metadata(group))
                prepared = estimate(retained + groups[index + 1 :])
                if prepared <= budget:
                    retained.extend(groups[index + 1 :])
                    break
            groups = retained
            prepared = estimate()
        if prepared > budget and active:
            results = [
                entry
                for entry in active["entries"]
                if entry["message"]["role"] == "tool"
                and not is_summary(entry["message"]["content"])
            ]
            results.sort(
                key=lambda entry: (-len(utf8(entry["message"]["content"])), entry["index"])
            )
            for entry in results:
                if prepared <= budget:
                    break
                summarize_entry(entry)
        prepared = estimate()
        compaction = {
            "applied": bool(capsule_telemetry or removed or summaries or dropped),
            "removedReasoning": removed,
            "summarizedToolResults": summaries,
            "droppedGroups": dropped,
            "protectedContext": {
                "systemMessageIndexes": systems,
                **({"latestUserMessageIndex": latest_user} if latest_user is not None else {}),
                "activeToolClusterMessageIndexes": active_indices,
            },
            **({"contextCapsule": capsule_telemetry} if capsule_telemetry else {}),
        }
        estimation = {
            **self.options,
            "inputBudgetTokens": budget,
            "originalInputTokens": original,
            "preparedInputTokens": prepared,
        }
        if prepared > budget:
            raise ContextError(
                "CONTEXT_BUDGET_EXCEEDED",
                f"Prepared context requires {prepared} estimated tokens but the input budget is {budget}",
                {
                    **estimation,
                    "requiredReductionTokens": prepared - budget,
                    "protectedInputTokens": self.estimate(
                        flatten([group for group in groups if group["protected"]]) + local, tools
                    ),
                    "compaction": compaction,
                },
            )
        return {
            "messages": deepcopy(flatten(groups)),
            "requestLocalMessages": local,
            "estimate": estimation,
            "compaction": compaction,
        }
