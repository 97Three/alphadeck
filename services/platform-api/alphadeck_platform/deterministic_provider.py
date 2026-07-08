"""Execute an immutable platform tool plan through the ordinary Agent ledger."""

import asyncio
import json
from contextlib import suppress
from copy import deepcopy

from .provenance import js_json


class DeterministicProvider:
    def __init__(self, steps, *, name="pi-agent-deterministic"):
        if not steps:
            raise ValueError("A deterministic PI Agent tool plan requires at least one step.")
        if any(not isinstance(step.get("name"), str) or not step["name"].strip() for step in steps):
            raise ValueError("Deterministic PI Agent tool steps require a non-empty tool name.")
        self.name, self.steps, self.cursor, self.issued = name, deepcopy(steps), 0, False

    async def complete(self, request):
        cancel = request.get("signal") or request.get("cancel")
        if cancel and cancel.is_set():
            raise asyncio.CancelledError
        if self.issued:
            previous = self.steps[self.cursor]
            message = next(
                (value for value in reversed(request["messages"]) if value["role"] == "tool"), None
            )
            outcome = False
            if (
                message
                and message.get("name") == previous["name"]
                and message.get("toolCallId") == f"deterministic_step_{self.cursor + 1}"
            ):
                with suppress(ValueError, TypeError, AttributeError):
                    outcome = json.loads(message["content"]).get("ok") is True
            if not outcome:
                yield {
                    "type": "response_start",
                    "responseId": f"deterministic_failed_{self.cursor}",
                    "model": request["model"],
                }
                yield {
                    "type": "text_delta",
                    "delta": "The trusted deterministic step failed; the run is stopping for platform recovery.",
                }
                yield {
                    "type": "usage",
                    "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                }
                yield {"type": "finish", "reason": "stop"}
                return
            self.cursor += 1
            self.issued = False
        if self.cursor >= len(self.steps):
            yield {
                "type": "response_start",
                "responseId": f"deterministic_complete_{self.cursor}",
                "model": request["model"],
            }
            yield {
                "type": "usage",
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
            }
            yield {"type": "finish", "reason": "stop"}
            return
        step = self.steps[self.cursor]
        if not any(value["name"] == step["name"] for value in request.get("tools", [])):
            raise ValueError(
                "Deterministic PI Agent step requires unavailable tool: " + step["name"]
            )
        self.issued = True
        number = self.cursor + 1
        yield {
            "type": "response_start",
            "responseId": f"deterministic_{number}",
            "model": request["model"],
        }
        yield {
            "type": "tool_call_delta",
            "index": 0,
            "id": f"deterministic_step_{number}",
            "nameDelta": step["name"],
            "argumentsDelta": js_json(step["arguments"]),
        }
        yield {"type": "usage", "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}}
        yield {"type": "finish", "reason": "tool_calls"}
