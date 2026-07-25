import asyncio
import hashlib
import json
import math
import re
from copy import deepcopy
from urllib.parse import quote_plus, unquote

import httpx

from .config import ROOT, market_base_url
from .data_quality import assess_response
from .file_tools import record
from .finance_values import js_string
from .messages import js_length
from .prefetch_market import timestamp
from .provenance import js_json
from .quant_output import compact_quant_output
from .research_time import ResearchTimeError, constrain_historical_query
from .tool_paths import ToolError

CATALOG = json.loads((ROOT / "config/quant-api-tool-catalog.json").read_text())
ALLOWED = [re.compile(pattern) for pattern in CATALOG["allowedPaths"]]


def query_input(value):
    value = record(value)
    if len(value) > 100:
        raise ToolError("INVALID_TOOL_INPUT", "query accepts at most 100 keys.")

    def primitive(item):
        return (
            isinstance(item, str)
            and js_length(item) <= 2048
            or type(item) is bool
            or type(item) in {int, float}
            and math.isfinite(item)
        )

    for key, item in value.items():
        if not key or js_length(key) > 200 or re.search(r"[\r\n\x00]", key):
            raise ToolError("INVALID_TOOL_INPUT", "Query keys must be 1-200 printable characters.")
        if not primitive(item) and not (
            isinstance(item, list) and len(item) <= 100 and all(primitive(child) for child in item)
        ):
            raise ToolError(
                "INVALID_TOOL_INPUT",
                f"query.{key} must be a string, finite number, boolean, or an array of those values.",
            )
    return value


def parse_quant_input(value):
    value = record(value)
    query = query_input(value["query"]) if "query" in value else {}
    path = value.get("path")
    if not isinstance(path, str) or not path:
        raise ToolError("INVALID_TOOL_INPUT", "path must be a non-empty string.")
    if js_length(path) > 2048:
        raise ToolError("INVALID_TOOL_INPUT", "path exceeds 2048 characters.")
    return {"path": path, "query": query}


def encoded_query(pairs):
    def encode(value):
        return quote_plus(value, safe="*", errors="replace").replace("~", "%7E")

    return "&".join(encode(key) + "=" + encode(value) for key, value in pairs)


def quant_target(path, query):
    if not path.startswith("/api/v1/") or path.startswith("//"):
        raise ToolError(
            "QUANT_API_PATH_DENIED", "quant_api_get only accepts paths beginning with /api/v1/."
        )
    if re.search(r"[\\?#\r\n\x00]", path):
        raise ToolError(
            "QUANT_API_PATH_DENIED",
            "The API path must not contain query text, fragments, backslashes, or control characters.",
        )
    for raw in path.split("/"):
        if re.search(r"%(?![a-fA-F0-9]{2})", raw):
            raise ToolError(
                "QUANT_API_PATH_DENIED", "The API path contains malformed percent encoding."
            )
        try:
            decoded = unquote(raw, errors="strict")
        except UnicodeError as cause:
            raise ToolError(
                "QUANT_API_PATH_DENIED", "The API path contains malformed percent encoding."
            ) from cause
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded:
            raise ToolError(
                "QUANT_API_PATH_DENIED", "API path traversal and encoded separators are denied."
            )
    if not any(pattern.fullmatch(path) for pattern in ALLOWED):
        raise ToolError(
            "QUANT_API_ENDPOINT_DENIED",
            "The requested local API endpoint is not in the PI Agent read-only quant allowlist.",
        )
    pairs = [
        (key, js_string(item))
        for key, value in query.items()
        for item in (value if isinstance(value, list) else [value])
    ]
    suffix = encoded_query(pairs)
    # The fixed product service port moves with the isolated Python copy.
    target = market_base_url().rstrip("/") + path + ("?" + suffix if suffix else "")
    if js_length(target) > 8192:
        raise ToolError("QUANT_API_URL_TOO_LONG", "Quant API URLs cannot exceed 8192 characters.")
    canonical = encoded_query(
        sorted(pairs, key=lambda pair: pair[0].encode("utf-16-be", "surrogatepass"))
    )
    return target, path + ("?" + canonical if canonical else "")


class QuantApiTool:
    def __init__(
        self,
        *,
        research_time=None,
        client=None,
        timeout_ms=15000,
        max_output_chars=12000,
        max_response_bytes=2000000,
        max_requests=32,
    ):
        if any(
            type(value) is not int or value <= 0
            for value in (timeout_ms, max_output_chars, max_response_bytes, max_requests)
        ):
            raise ValueError("Quant tool budgets must be positive integers")
        self.time, self.client = research_time, client
        self.timeout, self.output, self.bytes, self.requests = (
            timeout_ms,
            max_output_chars,
            max_response_bytes,
            max_requests,
        )
        self.count = 0
        self.definition = deepcopy(CATALOG["definition"])

    async def execute(self, value, context=None):
        try:
            input = parse_quant_input(value)
            async with asyncio.timeout(self.timeout / 1000):
                return await self._execute(input)
        except TimeoutError:
            error = ToolError("TOOL_TIMEOUT", f"PI Agent tool timed out after {self.timeout}ms.")
        except ResearchTimeError as cause:
            error = ToolError(cause.code, str(cause))
        except ToolError as cause:
            error = cause
        except Exception as cause:
            error = ToolError("TOOL_EXECUTION_FAILED", str(cause))
        return {
            "ok": False,
            "error": {
                "code": error.code,
                "message": error.message,
                **({"details": error.details} if error.details is not None else {}),
            },
            "content": error.message,
        }

    async def _execute(self, input):
        self.count += 1
        if self.count > self.requests:
            raise ToolError(
                "QUANT_API_REQUEST_BUDGET_EXCEEDED",
                f"This PI Agent run exceeded its {self.requests}-request quant API budget.",
            )
        query = constrain_historical_query(input["path"], input["query"], self.time)
        url, canonical = quant_target(input["path"], query)
        owned = self.client is None
        client = self.client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        try:
            async with client.stream(
                "GET",
                url,
                headers={"accept": "application/json"},
                timeout=self.timeout / 1000,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise ToolError("TOOL_EXECUTION_FAILED", "Quant API redirects are denied.")
                content, truncated = bytearray(), False
                async for chunk in response.aiter_bytes():
                    remaining = self.bytes - len(content)
                    if len(chunk) > remaining:
                        content.extend(chunk[:remaining])
                        truncated = True
                        break
                    content.extend(chunk)
                text, size = bytes(content).decode("utf-8", "replace"), len(content)

                def compact(value, byte_limit=truncated):
                    return compact_quant_output(
                        value,
                        limit=self.output,
                        response_bytes=size,
                        response_byte_limit_reached=byte_limit,
                        path=input["path"],
                    )

                output = compact(text)
                if not response.is_success:
                    return {
                        "ok": False,
                        "error": {
                            "code": "QUANT_API_HTTP_ERROR",
                            "message": f"Local quant API returned HTTP {response.status_code}.",
                            "details": {
                                "status": response.status_code,
                                "bodyTruncated": output["truncated"],
                                **(
                                    {"outputStrategy": output["strategy"]}
                                    if "strategy" in output
                                    else {}
                                ),
                            },
                        },
                        "content": output["text"],
                    }
                if truncated:
                    return {
                        "ok": False,
                        "error": {
                            "code": "QUANT_API_INCOMPLETE_RESPONSE",
                            "message": "The response exceeded the byte budget. Retry with a narrower date range or smaller limit.",
                        },
                        "content": output["text"],
                    }
                try:
                    payload = json.loads(
                        text,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON")),
                    )
                except ValueError:
                    return {
                        "ok": False,
                        "error": {
                            "code": "QUANT_API_INVALID_JSON",
                            "message": "The local API did not return valid JSON.",
                        },
                    }
                assessment = assess_response(input["path"], payload, query)
                if assessment["status"] == "failed":
                    return {
                        "ok": False,
                        "error": {
                            "code": "QUANT_API_DATA_INVALID",
                            "message": "Market data failed consistency checks: "
                            + ", ".join(
                                issue["code"] + "@" + issue["path"]
                                for issue in assessment["issues"]
                            ),
                            "details": {"assessment": assessment},
                        },
                    }
                now = timestamp()
                value = payload if isinstance(payload, dict) else {}
                fetched = value.get("fetched_at")
                receipt = {
                    "sourceId": value["source"]
                    if isinstance(value.get("source"), str)
                    else "local-market-api",
                    "operationId": "GET " + input["path"],
                    "observedAt": now,
                    "fetchedAt": fetched if isinstance(fetched, str) else now,
                    "fetchedAtOrigin": "source" if isinstance(fetched, str) else "observed",
                    **({"asOf": value["as_of"]} if isinstance(value.get("as_of"), str) else {}),
                    "querySha256": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
                    "responseSha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                }
                if assessment["status"] == "warning":
                    output = compact(
                        js_json(
                            {
                                "$piAgent": {
                                    "kind": "quant_api_data_quality",
                                    "assessment": assessment,
                                },
                                "data": payload,
                            }
                        ),
                        False,
                    )
                return {
                    "ok": True,
                    "data": {
                        "status": response.status_code,
                        "bytes": size,
                        "truncated": output["truncated"],
                        "assessment": assessment,
                        "receipt": receipt,
                        **({"outputStrategy": output["strategy"]} if "strategy" in output else {}),
                    },
                    "content": output["text"],
                }
        finally:
            if owned:
                await client.aclose()
