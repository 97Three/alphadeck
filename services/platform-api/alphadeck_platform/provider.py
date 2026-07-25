import asyncio
import codecs
import json
import math
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from .messages import js_length

RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}
LIMITS = {
    "maxErrorBodyBytes": 16384,
    "maxRequestBytes": 2000000,
    "maxSseFrameChars": 1000000,
    "maxStreamBytes": 8000000,
    "maxTextChars": 128000,
    "maxReasoningChars": 256000,
    "maxToolArgumentChars": 64000,
    "maxToolCalls": 32,
}


class ProviderError(Exception):
    def __init__(
        self,
        code,
        message,
        *,
        status=None,
        response_body=None,
        request_id=None,
        retry_after_ms=None,
    ):
        self.code, self.message, self.status = code, message, status
        self.response_body, self.request_id, self.retry_after_ms = (
            response_body,
            request_id,
            retry_after_ms,
        )
        super().__init__(message)


def protocol(message):
    return ProviderError("PROTOCOL_ERROR", message)


def count(value):
    if (
        type(value) in {int, float}
        and math.isfinite(value)
        and value == math.floor(value)
        and 0 <= value <= 9007199254740991
    ):
        return int(value)
    return None


def parse_usage(value):
    if not isinstance(value, dict):
        return None
    prompt, completion, total = (
        count(value.get(key)) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    if prompt is None or completion is None or total is None or prompt + completion != total:
        return None
    hit, miss = (
        count(value.get("prompt_cache_hit_tokens")),
        count(value.get("prompt_cache_miss_tokens")),
    )
    standard = (
        count(value["prompt_tokens_details"].get("cached_tokens"))
        if isinstance(value.get("prompt_tokens_details"), dict)
        else None
    )
    reasoning = (
        count(value["completion_tokens_details"].get("reasoning_tokens"))
        if isinstance(value.get("completion_tokens_details"), dict)
        else None
    )
    if (
        (hit is None) != (miss is None)
        or (hit is not None and hit + miss != prompt)
        or (standard is not None and standard > prompt)
        or (reasoning is not None and reasoning > completion)
    ):
        return None
    cached = hit if hit is not None else standard
    missed = miss if miss is not None else prompt - standard if standard is not None else None
    return {
        "inputTokens": prompt,
        "outputTokens": completion,
        "totalTokens": total,
        "cachedInputTokens": cached if cached is not None else 0,
        "cacheMissInputTokens": missed if missed is not None else prompt,
        **({"reasoningTokens": reasoning} if reasoning is not None else {}),
        **({"usageSource": "cache_estimated"} if cached is None else {}),
    }


def retry_after(value):
    if not value:
        return None
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return math.floor(seconds * 1000 + 0.5)
    except ValueError:
        pass
    try:
        timestamp = parsedate_to_datetime(value)
        timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
        return max(0, math.floor((timestamp - datetime.now(UTC)).total_seconds() * 1000 + 0.5))
    except (ValueError, TypeError, OverflowError):
        return None


def request_body(request, *, deepseek=True):
    messages = []
    for message in request["messages"]:
        role = message["role"]
        serialized = {"role": role, "content": message["content"]}
        if role == "assistant":
            if deepseek and "reasoningContent" in message:
                serialized["reasoning_content"] = message["reasoningContent"]
            if message.get("toolCalls"):
                serialized["tool_calls"] = [
                    {
                        "id": tool["id"],
                        "type": "function",
                        "function": {"name": tool["name"], "arguments": tool["arguments"]},
                    }
                    for tool in message["toolCalls"]
                ]
        if role == "tool":
            serialized["tool_call_id"] = message["toolCallId"]
        messages.append(serialized)
    if not deepseek:
        leading = next(
            (index for index, item in enumerate(messages) if item["role"] != "system"),
            len(messages),
        )
        if leading > 1:
            messages = [
                {
                    "role": "system",
                    "content": "\n\n".join(item["content"] for item in messages[:leading]),
                }
            ] + messages[leading:]
    body = {
        "model": "deepseek-flash" if request["model"] == "deepseek-v4-flash" else request["model"],
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.get("tools"):
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["inputSchema"],
                },
            }
            for tool in request["tools"]
        ]
    if "toolChoice" in request:
        choice = request["toolChoice"]
        body["tool_choice"] = (
            choice
            if isinstance(choice, str)
            else {"type": "function", "function": {"name": choice["name"]}}
        )
    for key, field in (("maxTokens", "max_tokens"), ("temperature", "temperature")):
        if key in request:
            body[field] = request[key]
    if deepseek and "reasoning" in request:
        reasoning = request["reasoning"]
        body["thinking"] = {"type": "enabled" if reasoning["enabled"] else "disabled"}
        if "effort" in reasoning:
            body["reasoning_effort"] = reasoning["effort"]
    return body


async def sse_data(response, limits):
    decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="replace")
    buffer = ""
    size = 0

    def frames(flush=False):
        nonlocal buffer
        buffer = buffer.replace("\r\n", "\n")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            yield frame
        if flush and buffer.strip():
            frame, buffer = buffer, ""
            yield frame

    def extract(frame):
        if js_length(frame) > limits["maxSseFrameChars"]:
            raise protocol("Model SSE frame exceeded its character budget.")
        lines = []
        for line in frame.split("\n"):
            if line == "data" or line.startswith("data:"):
                data = "" if line == "data" else line[5:]
                lines.append(data[1:] if data.startswith(" ") else data)
        return "\n".join(lines) if lines else None

    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limits["maxStreamBytes"]:
            raise protocol("Model stream exceeded its byte budget.")
        buffer += decoder.decode(chunk)
        for frame in frames():
            value = extract(frame)
            if value is not None:
                yield value
        if js_length(buffer) > limits["maxSseFrameChars"]:
            raise protocol("Model SSE frame exceeded its character budget.")
    buffer += decoder.decode(b"", final=True)
    for frame in frames(True):
        value = extract(frame)
        if value is not None:
            yield value


class ChatProvider:
    def __init__(
        self,
        api_key,
        base_url="https://api.deepseek.com",
        *,
        deepseek=True,
        headers=None,
        limits=None,
        max_retries=2,
        initial_delay_ms=500,
        max_delay_ms=10000,
        client=None,
        sleep=asyncio.sleep,
        random_source=random.random,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Model credentials are required")
        base = base_url.strip().rstrip("/")
        if not base:
            raise ValueError("Model base URL cannot be empty")
        self.url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        self.api_key, self.deepseek, self.headers = api_key, deepseek, headers or {}
        self.limits = {**LIMITS, **(limits or {})}
        if (
            any(type(value) is not int or value <= 0 for value in self.limits.values())
            or type(max_retries) is not int
            or max_retries < 0
            or any(
                type(value) is not int or value < 0 for value in (initial_delay_ms, max_delay_ms)
            )
        ):
            raise ValueError("Invalid model provider limits")
        self.max_retries, self.initial_delay_ms, self.max_delay_ms = (
            max_retries,
            initial_delay_ms,
            max_delay_ms,
        )
        self.client, self.sleep, self.random = client, sleep, random_source

    def delay(self, attempt, retry=None):
        if retry is not None:
            return min(self.max_delay_ms, max(0, retry))
        ceiling = min(self.max_delay_ms, self.initial_delay_ms * 2 ** min(30, max(0, attempt - 1)))
        return math.floor(ceiling * (0.5 + min(1, max(0, self.random())) * 0.5) + 0.5)

    async def complete(self, request):
        if self.client is None:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120, connect=10), follow_redirects=False
            ) as client:
                async for event in self._complete(request, client):
                    yield event
        else:
            async for event in self._complete(request, self.client):
                yield event

    async def _complete(self, request, client):
        body = json.dumps(
            request_body(request, deepseek=self.deepseek),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        if len(body) > self.limits["maxRequestBytes"]:
            raise ProviderError("REQUEST_TOO_LARGE", "Model request exceeded its byte budget.")
        headers = {
            **self.headers,
            "Accept": "text/event-stream",
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }
        response = None
        attempts = self.max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                candidate = await client.send(
                    client.build_request("POST", self.url, headers=headers, content=body),
                    stream=True,
                    follow_redirects=False,
                )
            except httpx.HTTPError as error:
                if attempt >= attempts:
                    raise ProviderError(
                        "NETWORK_ERROR", "Model request failed before a response was received."
                    ) from error
                delay = self.delay(attempt)
                yield {
                    "type": "provider_retry",
                    "attempt": attempt + 1,
                    "maxAttempts": attempts,
                    "delayMs": delay,
                    "code": "NETWORK_ERROR",
                }
                await self.sleep(delay / 1000)
                continue
            if candidate.is_success:
                response = candidate
                break
            retry = retry_after(candidate.headers.get("retry-after"))
            if candidate.status_code in RETRYABLE and attempt < attempts:
                await candidate.aclose()
                delay = self.delay(attempt, retry)
                yield {
                    "type": "provider_retry",
                    "attempt": attempt + 1,
                    "maxAttempts": attempts,
                    "delayMs": delay,
                    "code": "HTTP_ERROR",
                    "status": candidate.status_code,
                }
                await self.sleep(delay / 1000)
                continue
            collected = bytearray()
            try:
                async for chunk in candidate.aiter_bytes():
                    collected.extend(
                        chunk[: max(0, self.limits["maxErrorBodyBytes"] - len(collected))]
                    )
                    if len(collected) >= self.limits["maxErrorBodyBytes"]:
                        break
            finally:
                await candidate.aclose()
            raise ProviderError(
                "HTTP_ERROR",
                f"Model request failed with HTTP {candidate.status_code}.",
                status=candidate.status_code,
                response_body=collected.decode(errors="replace"),
                request_id=candidate.headers.get("x-request-id")
                or candidate.headers.get("request-id"),
                retry_after_ms=retry,
            )
        if response is None:
            raise ProviderError("NETWORK_ERROR", "Model retries ended without a response.")
        try:
            async for event in self.parse(response, request["model"]):
                yield event
        except httpx.HTTPError as error:
            # No retry after stream start: a tool call may already have been observed.
            raise ProviderError(
                "NETWORK_ERROR", "Model response stream was interrupted."
            ) from error
        finally:
            await response.aclose()

    async def parse(self, response, model):
        response_id = None
        finished = False
        text_chars, reasoning_chars = 0, 0
        arguments, names, ids = {}, {}, {}
        async for data in sse_data(response, self.limits):
            if data.strip() == "[DONE]":
                if not finished:
                    raise protocol("Model stream ended before a finish reason was received.")
                return
            try:
                chunk = json.loads(data)
            except ValueError as error:
                raise protocol("Model event stream contains malformed JSON.") from error
            if not isinstance(chunk, dict):
                raise protocol("Model event stream chunk must be an object.")
            current_id = (
                chunk.get("id")
                if isinstance(chunk.get("id"), str) and chunk["id"].strip()
                else None
            )
            usage = parse_usage(chunk.get("usage"))
            if chunk.get("usage") is not None and usage is None:
                raise protocol("Model returned inconsistent token usage.")
            choices = chunk.get("choices")
            semantic = usage is not None or isinstance(choices, list) and bool(choices)
            if semantic and not current_id:
                raise protocol("Semantic model chunks require a response ID.")
            if current_id and response_id and current_id != response_id:
                raise protocol("Model response ID changed within one stream.")
            if not response_id and current_id:
                response_id = current_id
                yield {
                    "type": "response_start",
                    "responseId": response_id,
                    "model": chunk["model"] if isinstance(chunk.get("model"), str) else model,
                }
            if usage is not None:
                yield {"type": "usage", "usage": usage}
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict) or count(choice.get("index")) != 0:
                    raise protocol("Model choice must have index 0.")
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_chars += js_length(reasoning)
                        if reasoning_chars > self.limits["maxReasoningChars"]:
                            raise protocol("Model reasoning exceeded its character budget.")
                        yield {"type": "reasoning_delta", "delta": reasoning}
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        text_chars += js_length(text)
                        if text_chars > self.limits["maxTextChars"]:
                            raise protocol("Model response text exceeded its character budget.")
                        yield {"type": "text_delta", "delta": text}
                    tools = delta.get("tool_calls")
                    for tool in tools if isinstance(tools, list) else []:
                        if not isinstance(tool, dict):
                            continue
                        index = count(tool.get("index"))
                        if index is None:
                            raise protocol("Tool-call delta requires a valid index.")
                        identity = (
                            tool.get("id")
                            if isinstance(tool.get("id"), str) and tool["id"].strip()
                            else None
                        )
                        if index not in arguments:
                            if not identity:
                                raise protocol("Tool-call stream must start with a non-empty ID.")
                            if len(arguments) >= self.limits["maxToolCalls"]:
                                raise protocol("Model response exceeded its tool-call budget.")
                            arguments[index], names[index] = 0, 0
                        if identity is not None:
                            if index in ids and ids[index] != identity:
                                raise protocol("Model changed a tool-call ID within one stream.")
                            ids[index] = identity
                        function = (
                            tool["function"] if isinstance(tool.get("function"), dict) else {}
                        )
                        event = {
                            "type": "tool_call_delta",
                            "index": index,
                            **({"id": identity} if identity is not None else {}),
                        }
                        for field, output, counters, maximum in (
                            ("name", "nameDelta", names, 256),
                            (
                                "arguments",
                                "argumentsDelta",
                                arguments,
                                self.limits["maxToolArgumentChars"],
                            ),
                        ):
                            if isinstance(function.get(field), str):
                                counters[index] += js_length(function[field])
                                if counters[index] > maximum:
                                    raise protocol(
                                        "Model tool name or arguments exceeded its character budget."
                                    )
                                event[output] = function[field]
                        yield event
                reason = choice.get("finish_reason")
                if isinstance(reason, str) and reason:
                    finished = True
                    normalized = {
                        "stop": "stop",
                        "tool_calls": "tool_calls",
                        "function_call": "tool_calls",
                        "length": "length",
                        "content_filter": "content_filter",
                        "insufficient_system_resource": "resource_exhausted",
                    }.get(reason, "other")
                    yield {"type": "finish", "reason": normalized, "rawReason": reason}
        if not finished:
            raise protocol("Model stream closed before a finish reason was received.")
