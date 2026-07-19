import asyncio
import base64
import codecs
import json
import math
import re
import time
from urllib.parse import quote, urlencode

import httpx

from .messages import js_length
from .provenance import js_json


class MemoryHttpError(Exception):
    def __init__(self, message, status=None, code="INVALID_RESPONSE", request_id=None):
        self.status, self.code, self.request_id = status, code, request_id
        super().__init__(message)


def correlation(value):
    return re.sub(r"[^A-Za-z0-9._:-]", "-", value or "")[:64] or None


def invalid(label):
    raise MemoryHttpError("Invalid " + label + " response.")


def checked(value, label, kind):
    valid = (
        isinstance(value, dict)
        if kind == "object"
        else isinstance(value, str) and bool(value)
        if kind == "text"
        else type(value) is bool
        if kind == "boolean"
        else isinstance(value, list) and all(isinstance(item, str) for item in value)
        if kind == "strings"
        else isinstance(value, dict) and all(isinstance(item, str) for item in value.values())
        if kind == "context"
        else type(value) in {int, float}
        and math.isfinite(value)
        and (kind != "integer" or value == math.floor(value))
    )
    if not valid:
        invalid(label)
    return value


def algorithm(value):
    value = checked(value, "algorithm", "text")
    if value not in {"ranked-extractive-v1", "exact-deduplicated-v1"}:
        invalid("projection algorithm")
    return value


def mapped(value, label, fields):
    value = checked(value, label, "object")
    result = {}
    for target, source, kind in fields:
        item = value.get(source)
        result[target] = (
            (None if item is None else checked(item, "optional text", "text"))
            if kind == "optional"
            else algorithm(item)
            if kind == "algorithm"
            else checked(item, source, kind)
        )
    return result


WRITE = [
    ("observationId", "observation_id", "text"),
    ("candidateId", "candidate_id", "text"),
    ("recordId", "record_id", "text"),
    ("revisionId", "revision_id", "text"),
    ("sequence", "sequence", "integer"),
    ("idempotentReplay", "idempotent_replay", "boolean"),
]
SUMMARY = [
    (key, source, kind)
    for key, source, kind in [
        ("recordId", "record_id", "text"),
        ("revisionId", "revision_id", "text"),
        ("sequence", "sequence", "integer"),
        ("key", "key", "text"),
        ("value", "value", "text"),
        ("context", "context", "context"),
        ("confidence", "confidence", "number"),
        ("supportCount", "support_count", "integer"),
        ("evidenceCount", "evidence_count", "integer"),
        ("validFrom", "valid_from", "text"),
        ("recordedAt", "recorded_at", "text"),
    ]
]
REVISION = [
    ("id", "id", "text"),
    ("sequence", "sequence", "integer"),
    ("value", "value", "text"),
    ("confidence", "confidence", "number"),
    ("supportCount", "support_count", "integer"),
    ("contradictionCount", "contradiction_count", "integer"),
    ("evidenceIds", "evidence_ids", "strings"),
    ("validFrom", "valid_from", "text"),
    ("recordedAt", "recorded_at", "text"),
    ("supersedesRevisionId", "supersedes_revision_id", "optional"),
]
USAGE = [
    ("usageId", "usage_id", "text"),
    ("traceId", "trace_id", "text"),
    ("algorithm", "algorithm", "algorithm"),
    ("maxCharacters", "max_characters", "integer"),
    ("sourceProjectionSha256", "source_projection_sha256", "text"),
    ("deliveredContextSha256", "delivered_context_sha256", "text"),
    ("revisionIds", "revision_ids", "strings"),
    ("occurredAt", "occurred_at", "text"),
    ("recordedAt", "recorded_at", "text"),
    ("idempotentReplay", "idempotent_replay", "boolean"),
]


async def bounded_request(
    client, method, url, headers, *, body=None, timeout=5, maximum=1500000, broker=False
):
    correlation_id = headers.get("X-Request-ID")
    prefix = "Personal memory token broker" if broker else "External memory"
    try:
        async with (
            asyncio.timeout(timeout),
            client.stream(
                method,
                url,
                headers=headers,
                content=js_json(body).encode() if body is not None else None,
                follow_redirects=False,
                timeout=timeout,
            ) as response,
        ):
            response_id = response.headers.get("x-request-id") or correlation_id
            decoder, chunks, chars = codecs.getincrementaldecoder("utf-8-sig")("replace"), [], 0
            async for block in response.aiter_bytes():
                text = decoder.decode(block)
                chars += js_length(text)
                if chars > maximum:
                    raise MemoryHttpError(
                        prefix + " response exceeded the client limit.",
                        response.status_code,
                        "TOKEN_BROKER_INVALID_RESPONSE" if broker else "RESPONSE_TOO_LARGE",
                        response_id,
                    )
                chunks.append(text)
            tail = decoder.decode(b"", final=True)
            if chars + js_length(tail) > maximum:
                raise MemoryHttpError(
                    prefix + " response exceeded the client limit.",
                    response.status_code,
                    "TOKEN_BROKER_INVALID_RESPONSE" if broker else "RESPONSE_TOO_LARGE",
                    response_id,
                )
            raw = "".join(chunks) + tail
            try:
                payload = (
                    json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if raw or broker
                    else None
                )
            except ValueError as cause:
                raise MemoryHttpError(
                    prefix + " returned invalid JSON.",
                    response.status_code,
                    "TOKEN_BROKER_INVALID_RESPONSE" if broker else "INVALID_JSON",
                    response_id,
                ) from cause
            if not response.is_success:
                raise MemoryHttpError(
                    prefix + (" rejected the request." if broker else " request was rejected."),
                    response.status_code,
                    "TOKEN_BROKER_REJECTED"
                    if broker
                    else payload.get("error", "HTTP_ERROR")
                    if isinstance(payload, dict) and isinstance(payload.get("error"), str)
                    else "HTTP_ERROR",
                    response_id,
                )
            return payload, response.status_code, response_id
    except (httpx.HTTPError, TimeoutError) as cause:
        timed_out = isinstance(cause, (httpx.TimeoutException, TimeoutError))
        raise MemoryHttpError(
            prefix + (" is unavailable." if broker else " service is unavailable."),
            None,
            ("TOKEN_BROKER_" if broker else "") + ("TIMEOUT" if timed_out else "NETWORK_ERROR"),
            correlation_id,
        ) from cause


class MemoryTokens:
    def __init__(self, config, client, *, clock=time.time):
        self.config, self.client, self.clock, self.cache = config, client, clock, {}

    async def token_for(self, scope, request_id=None):
        broker = self.config.get("tokenBroker")
        if not broker:
            return self.config.get("bearerToken")
        key = tuple(scope[field] for field in ("tenantId", "subjectId", "purpose"))
        cached = self.cache.get(key)
        if cached and cached[1] - 30 > self.clock():
            return cached[0]
        correlation_id = correlation(request_id)
        payload, status, response_id = await bounded_request(
            self.client,
            "POST",
            broker["url"],
            {
                "Accept": "application/json",
                "Authorization": "Basic "
                + base64.b64encode(
                    (broker["clientId"] + ":" + broker["clientSecret"]).encode()
                ).decode(),
                "Content-Type": "application/json",
                **({"X-Request-ID": correlation_id} if correlation_id else {}),
            },
            body={
                "audience": broker["audience"],
                "tenant_id": scope["tenantId"],
                "subject_id": scope["subjectId"],
                "purpose": scope["purpose"],
                "requested_role": "subject_self",
            },
            timeout=self.config["timeoutMs"] / 1000,
            maximum=65536,
            broker=True,
        )
        payload = payload if isinstance(payload, dict) else {}
        token = payload.get("access_token", "")
        token = token.strip() if isinstance(token, str) else ""
        expires = payload.get("expires_in")
        if (
            not token
            or js_length(token) > 16384
            or not isinstance(payload.get("token_type"), str)
            or payload["token_type"].lower() != "bearer"
            or type(expires) not in {int, float}
            or not math.isfinite(expires)
            or not 60 <= expires <= 3600
        ):
            raise MemoryHttpError(
                "Personal memory token broker returned an invalid token.",
                status,
                "TOKEN_BROKER_INVALID_RESPONSE",
                response_id,
            )
        if len(self.cache) >= 1000:
            del self.cache[next(iter(self.cache))]
        self.cache[key] = (token, self.clock() + expires)
        return token


class MemoryHttp:
    def __init__(self, config, client=None, tokens=None):
        self.config, self.client, self.tokens = config, client, tokens
        self._owned = client is None

    async def __aenter__(self):
        if self.client is None:
            self.client = httpx.AsyncClient(follow_redirects=False)
        if self.tokens is None:
            self.tokens = MemoryTokens(self.config, self.client)
        return self

    async def __aexit__(self, *_):
        if self._owned and self.client:
            await self.client.aclose()

    async def request(self, path, *, body=None, query=None, scope=None, request_id=None):
        if self.client is None:
            raise ValueError("Memory HTTP adapter must be opened before use")
        headers = {"Accept": "application/json"}
        if value := correlation(request_id):
            headers["X-Request-ID"] = value
        if scope and (token := await self.tokens.token_for(scope, request_id)):
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = self.config["apiUrl"].rstrip("/") + "/" + path.lstrip("/")
        if query:
            url += "?" + urlencode(query, safe="*").replace("~", "%7E")
        return (
            await bounded_request(
                self.client,
                "POST" if body is not None else "GET",
                url,
                headers,
                body=body,
                timeout=self.config["timeoutMs"] / 1000,
            )
        )[0]

    async def discover(self, request_id=None):
        data = checked(
            await self.request("/", request_id=request_id), "service discovery", "object"
        )
        contract = checked(data.get("api_contract"), "api_contract", "text")
        if contract != self.config["expectedContract"]:
            raise MemoryHttpError(
                "External memory contract is incompatible.",
                200,
                "INCOMPATIBLE_CONTRACT",
                correlation(request_id),
            )
        return mapped(
            data,
            "service discovery",
            [
                ("name", "name", "text"),
                ("version", "version", "text"),
                ("apiContract", "api_contract", "text"),
                ("capabilities", "capabilities", "strings"),
                ("authMode", "auth_mode", "text"),
                ("scopeSource", "scope_source", "text"),
                ("productionReady", "production_ready", "boolean"),
                ("productionBlockers", "production_blockers", "strings"),
            ],
        )

    async def check_ready(self, request_id=None):
        data = checked(await self.request("/readyz", request_id=request_id), "readiness", "object")
        if data.get("status") != "ready":
            raise MemoryHttpError(
                "External memory service is not ready.", 503, "NOT_READY", correlation(request_id)
            )

    @staticmethod
    def wire(input, fields):
        return {target: input[source] for source, target in fields if source in input}

    async def list_preferences(self, input, request_id=None):
        data = await self.request(
            "/v1/preferences",
            query=self.wire(
                input,
                [("tenantId", "tenant_id"), ("subjectId", "subject_id"), ("purpose", "purpose")],
            ),
            scope=input,
            request_id=request_id,
        )
        if not isinstance(data, list):
            raise MemoryHttpError("Invalid preference list.")
        return [mapped(item, "preference summary", SUMMARY) for item in data]

    async def remember_preference(self, input, request_id=None):
        body = self.wire(
            input,
            [
                ("tenantId", "tenant_id"),
                ("subjectId", "subject_id"),
                ("source", "source"),
                ("idempotencyKey", "idempotency_key"),
                ("key", "key"),
                ("value", "value"),
                ("context", "context"),
                ("evidenceText", "evidence_text"),
                ("confidence", "confidence"),
                ("purpose", "purpose"),
                ("occurredAt", "occurred_at"),
            ],
        )
        return mapped(
            await self.request("/v1/preferences", body=body, scope=input, request_id=request_id),
            "memory write",
            WRITE,
        )

    async def correct_preference(self, input, request_id=None):
        body = self.wire(
            input,
            [
                ("tenantId", "tenant_id"),
                ("subjectId", "subject_id"),
                ("source", "source"),
                ("idempotencyKey", "idempotency_key"),
                ("value", "value"),
                ("evidenceText", "evidence_text"),
                ("reason", "reason"),
                ("purpose", "purpose"),
                ("occurredAt", "occurred_at"),
                ("expectedRevisionId", "expected_revision_id"),
            ],
        )
        return mapped(
            await self.request(
                "/v1/preferences/" + quote(input["recordId"], safe="~!*'()-._") + "/corrections",
                body=body,
                scope=input,
                request_id=request_id,
            ),
            "memory write",
            WRITE,
        )

    async def get_revisions(self, input, request_id=None):
        data = await self.request(
            "/v1/preferences/" + quote(input["recordId"], safe="~!*'()-._") + "/revisions",
            query=self.wire(
                input,
                [("tenantId", "tenant_id"), ("subjectId", "subject_id"), ("purpose", "purpose")],
            ),
            scope=input,
            request_id=request_id,
        )
        if not isinstance(data, list):
            raise MemoryHttpError("Invalid revision list.")
        return [mapped(item, "revision", REVISION) for item in data]

    async def recall(self, input, request_id=None):
        data = checked(
            await self.request(
                "/v1/recall",
                body=self.wire(
                    input,
                    [
                        ("tenantId", "tenant_id"),
                        ("subjectId", "subject_id"),
                        ("query", "query"),
                        ("context", "context"),
                        ("limit", "limit"),
                        ("purpose", "purpose"),
                    ],
                ),
                scope=input,
                request_id=request_id,
            ),
            "recall",
            "object",
        )
        if not isinstance(data.get("items"), list):
            raise MemoryHttpError("Invalid recall items.")
        result = mapped(
            data,
            "recall",
            [
                ("traceId", "trace_id", "text"),
                ("policyId", "policy_id", "text"),
                ("policyVersion", "policy_version", "integer"),
                ("validAt", "valid_at", "text"),
                ("knownAt", "known_at", "text"),
                ("createdAt", "created_at", "text"),
            ],
        )
        result["items"] = [
            mapped(
                item,
                "recall item",
                [
                    ("recordId", "record_id", "text"),
                    ("revisionId", "revision_id", "text"),
                    ("key", "key", "text"),
                    ("value", "value", "text"),
                    ("context", "context", "context"),
                    ("rank", "rank", "integer"),
                    ("score", "score", "number"),
                ],
            )
            for item in data["items"]
        ]
        return result

    async def project_context(self, input, request_id=None):
        data = checked(
            await self.request(
                "/v1/recall-contexts",
                body=self.wire(
                    input,
                    [
                        ("tenantId", "tenant_id"),
                        ("subjectId", "subject_id"),
                        ("traceId", "trace_id"),
                        ("algorithm", "algorithm"),
                        ("maxCharacters", "max_characters"),
                        ("purpose", "purpose"),
                    ],
                ),
                scope=input,
                request_id=request_id,
            ),
            "context projection",
            "object",
        )
        if not isinstance(data.get("segments"), list):
            raise MemoryHttpError("Invalid projection segments.")
        result = mapped(
            data,
            "context projection",
            [
                ("traceId", "trace_id", "text"),
                ("policyId", "policy_id", "text"),
                ("policyVersion", "policy_version", "integer"),
                ("content", "content", "text"),
                ("sourceRevisionIds", "source_revision_ids", "strings"),
                ("projectionSha256", "projection_sha256", "text"),
            ],
        )
        segments = []
        for segment in data["segments"]:
            segment = checked(segment, "projection segment", "object")
            if not isinstance(segment.get("sources"), list):
                raise MemoryHttpError("Invalid projection sources.")
            segments.append(
                {
                    "content": checked(segment.get("content"), "segment content", "text"),
                    "sources": [
                        mapped(
                            source,
                            "projection source",
                            [
                                ("recordId", "record_id", "text"),
                                ("revisionId", "revision_id", "text"),
                                ("rank", "rank", "integer"),
                                ("score", "score", "number"),
                            ],
                        )
                        for source in segment["sources"]
                    ],
                }
            )
        result["segments"] = segments
        return result

    async def record_usage(self, input, request_id=None):
        fields = [
            ("tenantId", "tenant_id"),
            ("subjectId", "subject_id"),
            ("traceId", "trace_id"),
            ("algorithm", "algorithm"),
            ("maxCharacters", "max_characters"),
            ("sourceProjectionSha256", "source_projection_sha256"),
            ("deliveredContextSha256", "delivered_context_sha256"),
            ("revisionIds", "revision_ids"),
            ("idempotencyKey", "idempotency_key"),
            ("purpose", "purpose"),
            ("occurredAt", "occurred_at"),
        ]
        return mapped(
            await self.request(
                "/v1/usages", body=self.wire(input, fields), scope=input, request_id=request_id
            ),
            "memory usage",
            USAGE,
        )

    async def record_outcome(self, input, request_id=None):
        fields = [
            ("tenantId", "tenant_id"),
            ("subjectId", "subject_id"),
            ("traceId", "trace_id"),
            ("revisionId", "revision_id"),
            ("usageId", "usage_id"),
            ("kind", "kind"),
            ("idempotencyKey", "idempotency_key"),
            ("weight", "weight"),
            ("purpose", "purpose"),
            ("note", "note"),
            ("occurredAt", "occurred_at"),
        ]
        return mapped(
            await self.request(
                "/v1/outcomes", body=self.wire(input, fields), scope=input, request_id=request_id
            ),
            "outcome",
            [
                ("outcomeId", "outcome_id", "text"),
                ("idempotentReplay", "idempotent_replay", "boolean"),
            ],
        )
