import asyncio
import base64
import codecs
import json
import math
import time
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from .memory_http import correlation
from .messages import js_length
from .provenance import js_json


class KnowledgeHttpError(Exception):
    def __init__(
        self, message, status=None, code="INVALID_RESPONSE", request_id=None, trace_id=None
    ):
        self.status, self.code, self.request_id, self.trace_id = status, code, request_id, trace_id
        super().__init__(message)


class KnowledgeIntegrationError(Exception):
    def __init__(self, code, status, message):
        self.code, self.status = code, status
        super().__init__(message)


def checked(value, label, kind):
    valid = (
        isinstance(value, dict)
        if kind == "object"
        else isinstance(value, str) and bool(value.strip())
        if kind == "text"
        else type(value) is bool
        if kind == "boolean"
        else isinstance(value, list) and all(isinstance(item, str) for item in value)
        if kind == "strings"
        else type(value) in {int, float} and math.isfinite(value)
    )
    if not valid:
        raise KnowledgeHttpError("Invalid " + label + " response.")
    return value


def mapped(value, label, fields):
    value = checked(value, label, "object")
    return {key: checked(value.get(key), key, kind) for key, kind in fields}


def citation(value):
    value = checked(value, "citation", "object")
    result = mapped(
        value,
        "citation",
        [
            (key, "text")
            for key in (
                "citationId",
                "chunkId",
                "payloadDigest",
                "recordId",
                "revisionId",
                "spaceId",
            )
        ],
    )
    result.update(
        locator=checked(value.get("locator"), "locator", "object"),
        quote=value.get("quote") if isinstance(value.get("quote"), str) else "",
    )
    return {
        key: result[key]
        for key in (
            "citationId",
            "chunkId",
            "payloadDigest",
            "locator",
            "quote",
            "recordId",
            "revisionId",
            "spaceId",
        )
    }


def passage(value):
    value = checked(value, "passage", "object")
    result = mapped(
        value,
        "passage",
        [
            ("citationId", "text"),
            ("chunkId", "text"),
            ("rank", "number"),
            ("recordId", "text"),
            ("revisionId", "text"),
            ("score", "number"),
            ("spaceId", "text"),
            ("title", "text"),
        ],
    )
    result["text"] = value.get("text") if isinstance(value.get("text"), str) else ""
    return {
        key: result[key]
        for key in (
            "citationId",
            "chunkId",
            "rank",
            "recordId",
            "revisionId",
            "score",
            "spaceId",
            "text",
            "title",
        )
    }


def context_pack(value):
    value = checked(value, "context pack", "object")
    quality = checked(value.get("quality"), "context quality", "object")
    decision = checked(quality.get("decision"), "quality decision", "text")
    if decision not in {"suitable", "suitable_with_warning", "insufficient"}:
        raise KnowledgeHttpError("Invalid context quality decision.")
    citations = (
        [citation(item) for item in value["citations"]]
        if isinstance(value.get("citations"), list)
        else []
    )
    passages = (
        [passage(item) for item in value["passages"]]
        if isinstance(value.get("passages"), list)
        else []
    )
    if any(
        item["citationId"] not in {item["citationId"] for item in citations} for item in passages
    ):
        raise KnowledgeHttpError("Context passages are not covered by citations.")
    result = mapped(
        value,
        "context pack",
        [
            (key, "text")
            for key in (
                "contextPackId",
                "contextDigest",
                "createdAt",
                "exposureReceiptId",
                "policyEpoch",
                "purpose",
            )
        ],
    )
    warnings = []
    for item in value["warnings"] if isinstance(value.get("warnings"), list) else []:
        warning = checked(item, "warning", "object")
        warnings.append(
            {
                "code": checked(warning.get("code"), "warning code", "text"),
                "message": checked(warning.get("message"), "warning message", "text"),
                "revisionIds": checked(warning["revisionIds"], "warning revisionIds", "strings")
                if "revisionIds" in warning
                else [],
            }
        )
    result.update(
        passages=passages,
        citations=citations,
        obligations=list(value["obligations"])
        if isinstance(value.get("obligations"), list)
        else [],
        quality={
            "decision": decision,
            "reasons": checked(quality.get("reasons"), "quality reasons", "strings"),
            "citationCoverage": checked(
                quality.get("citationCoverage"), "citation coverage", "number"
            ),
            "lexicalCoverage": checked(
                quality.get("lexicalCoverage"), "lexical coverage", "number"
            ),
        },
        warnings=warnings,
    )
    return result


async def json_request(
    client, method, url, headers, *, body=None, form=None, timeout=2, oauth=False
):
    maximum = 65536 if oauth else 1500000
    content = (
        urlencode(form, safe="*").replace("~", "%7E").encode()
        if form is not None
        else js_json(body).encode()
        if body is not None
        else None
    )
    request_id = headers.get("X-Request-ID")
    try:
        async with (
            asyncio.timeout(timeout),
            client.stream(
                method,
                url,
                headers=headers,
                content=content,
                timeout=timeout,
                follow_redirects=False,
            ) as response,
        ):
            response_id = response.headers.get("x-request-id") or request_id
            decoder, chunks, count = codecs.getincrementaldecoder("utf-8-sig")("replace"), [], 0
            async for chunk in response.aiter_bytes():
                text = decoder.decode(chunk)
                count += js_length(text)
                if count > maximum:
                    raise KnowledgeHttpError(
                        "Knowledge OAuth token response exceeded the client limit."
                        if oauth
                        else "Governed knowledge response exceeded the client limit.",
                        response.status_code,
                        "TOKEN_INVALID_RESPONSE" if oauth else "RESPONSE_TOO_LARGE",
                        response_id,
                    )
                chunks.append(text)
            tail = decoder.decode(b"", final=True)
            if count + js_length(tail) > maximum:
                raise KnowledgeHttpError(
                    "Knowledge OAuth token response exceeded the client limit."
                    if oauth
                    else "Governed knowledge response exceeded the client limit.",
                    response.status_code,
                    "TOKEN_INVALID_RESPONSE" if oauth else "RESPONSE_TOO_LARGE",
                    response_id,
                )
            raw = "".join(chunks) + tail
            try:
                payload = (
                    json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if raw or oauth
                    else None
                )
            except ValueError as cause:
                raise KnowledgeHttpError(
                    "Knowledge OAuth token endpoint returned invalid JSON."
                    if oauth
                    else "Governed knowledge service returned invalid JSON.",
                    response.status_code,
                    "TOKEN_INVALID_RESPONSE" if oauth else "INVALID_JSON",
                    response_id,
                ) from cause
            if not response.is_success:
                problem = payload if isinstance(payload, dict) else {}
                raise KnowledgeHttpError(
                    "Knowledge OAuth token request was rejected."
                    if oauth
                    else "Governed knowledge request was rejected.",
                    response.status_code,
                    "TOKEN_REJECTED"
                    if oauth
                    else problem["code"]
                    if isinstance(problem.get("code"), str)
                    else "HTTP_ERROR",
                    response_id,
                    problem.get("traceId") if isinstance(problem.get("traceId"), str) else None,
                )
            return payload, response.status_code, response_id
    except (httpx.HTTPError, TimeoutError) as cause:
        timed_out = isinstance(cause, (httpx.TimeoutException, TimeoutError))
        raise KnowledgeHttpError(
            "Knowledge OAuth token endpoint is unavailable."
            if oauth
            else "Governed knowledge service is unavailable.",
            None,
            ("TOKEN_" if oauth else "") + ("TIMEOUT" if timed_out else "NETWORK_ERROR"),
            request_id,
        ) from cause


class KnowledgeTokens:
    def __init__(self, config, client, *, clock=time.time):
        self.config, self.client, self.clock, self.cached = config, client, clock, None

    async def token_for(self, request_id=None):
        oauth = self.config.get("oauth")
        if not oauth:
            return self.config.get("bearerToken")
        if self.cached and self.cached[1] - 30 > self.clock():
            return self.cached[0]
        headers = {
            "Accept": "application/json",
            "Authorization": "Basic "
            + base64.b64encode((oauth["clientId"] + ":" + oauth["clientSecret"]).encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if request_id := correlation(request_id):
            headers["X-Request-ID"] = request_id
        payload, status, response_id = await json_request(
            self.client,
            "POST",
            oauth["tokenUrl"],
            headers,
            form={
                "grant_type": "client_credentials",
                "resource": oauth["resource"],
                "scope": oauth["scope"],
            },
            timeout=self.config["timeoutMs"] / 1000,
            oauth=True,
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
            raise KnowledgeHttpError(
                "Knowledge OAuth token endpoint returned an invalid token.",
                status,
                "TOKEN_INVALID_RESPONSE",
                response_id,
            )
        self.cached = (token, self.clock() + expires)
        return token


class KnowledgeHttp:
    def __init__(self, config, client=None, tokens=None):
        self.config, self.client, self.tokens, self.protocol_base = config, client, tokens, None
        self.owned = client is None

    async def __aenter__(self):
        if self.client is None:
            self.client = httpx.AsyncClient(follow_redirects=False)
        if self.tokens is None:
            self.tokens = KnowledgeTokens(self.config, self.client)
        return self

    async def __aexit__(self, *_):
        if self.owned and self.client:
            await self.client.aclose()

    def absolute(self, path):
        parsed = urlsplit(self.config["apiUrl"])
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    async def request(
        self, url, *, body=None, request_id=None, authenticated=True, idempotency_key=None
    ):
        if self.client is None:
            raise ValueError("Knowledge adapter must be opened before use")
        headers = {"Accept": "application/json"}
        if value := correlation(request_id):
            headers["X-Request-ID"] = value
        if authenticated and (token := await self.tokens.token_for(request_id)):
            headers["Authorization"] = "Bearer " + token
        if "/akep/" in urlsplit(url).path:
            headers["AKEP-Version"] = self.config["expectedVersion"]
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if body is not None:
            headers["Content-Type"] = "application/json"
        return (
            await json_request(
                self.client,
                "POST" if body is not None else "GET",
                url,
                headers,
                body=body,
                timeout=self.config["timeoutMs"] / 1000,
            )
        )[0]

    async def discover(self, request_id=None):
        configured = urlsplit(str(httpx.URL(self.config["apiUrl"])))
        data = checked(
            await self.request(
                self.absolute("/.well-known/akep"), request_id=request_id, authenticated=False
            ),
            "AKEP discovery",
            "object",
        )
        if checked(data.get("protocol"), "protocol", "text") != "akep":
            raise KnowledgeHttpError(
                "AKEP protocol is incompatible.",
                200,
                "INCOMPATIBLE_PROTOCOL",
                correlation(request_id),
            )
        advertised_url = str(httpx.URL(checked(data.get("baseUrl"), "baseUrl", "text")))
        advertised = urlsplit(advertised_url)
        loopbacks = {"localhost", "127.0.0.1", "::1"}
        if (
            configured.scheme != advertised.scheme
            or configured.port != advertised.port
            or (
                configured.hostname != advertised.hostname
                and not {configured.hostname, advertised.hostname} <= loopbacks
            )
            or not advertised.path.endswith("/akep/" + self.config["expectedVersion"])
        ):
            raise KnowledgeHttpError(
                "AKEP discovery advertised an unexpected protocol endpoint.",
                200,
                "UNTRUSTED_DISCOVERY_ENDPOINT",
                correlation(request_id),
            )
        self.protocol_base = advertised_url[:-1] if advertised_url.endswith("/") else advertised_url
        return {
            "protocol": "akep",
            **{
                key: checked(data.get(key), key, "strings")
                for key in ("versions", "operations", "profiles")
            },
            "supportedExtensions": [
                checked(checked(value, "extension", "object").get("uri"), "extension uri", "text")
                for value in data["supportedExtensions"]
            ]
            if isinstance(data.get("supportedExtensions"), list)
            else [],
            "baseUrl": self.protocol_base,
            "expiresAt": checked(data.get("expiresAt"), "expiresAt", "text"),
        }

    async def check_ready(self, request_id=None):
        data = checked(
            await self.request(
                self.absolute("/health/ready"), request_id=request_id, authenticated=False
            ),
            "AKEP readiness",
            "object",
        )
        if data.get("status") != "ready":
            raise KnowledgeHttpError(
                "Governed knowledge service is not ready.",
                503,
                "NOT_READY",
                correlation(request_id),
            )

    async def create_context_pack(self, input, request_id=None):
        if not self.protocol_base:
            await self.discover(request_id)
        body = {
            "akepVersion": self.config["expectedVersion"],
            "budget": {"maxCharacters": input["maxCharacters"]},
            "critical": [],
            "extensions": {},
            "mode": "lexical",
            **{key: input[key] for key in ("purpose", "spaces", "supportedObligations", "task")},
        }
        return context_pack(
            await self.request(
                self.protocol_base + "/context-packs", body=body, request_id=request_id
            )
        )

    async def record_usage(self, input, idempotency_key, request_id=None):
        if not self.protocol_base:
            await self.discover(request_id)
        data = await self.request(
            self.protocol_base + "/usages",
            body={
                "akepVersion": self.config["expectedVersion"],
                **input,
                "critical": [],
                "extensions": {},
            },
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        return mapped(
            data,
            "usage receipt",
            [
                (key, "text")
                for key in (
                    "usageId",
                    "exposureReceiptId",
                    "spaceId",
                    "policyEpoch",
                    "createdAt",
                    "feedbackUntil",
                )
            ],
        )

    async def record_feedback(self, input, idempotency_key, request_id=None):
        if not self.protocol_base:
            await self.discover(request_id)
        data = checked(
            await self.request(
                self.protocol_base + "/feedback",
                body={
                    "akepVersion": self.config["expectedVersion"],
                    **input,
                    "critical": [],
                    "extensions": {},
                },
                request_id=request_id,
                idempotency_key=idempotency_key,
            ),
            "feedback receipt",
            "object",
        )
        evaluator = checked(data.get("evaluatorVersion"), "evaluatorVersion", "object")
        status = checked(data.get("status"), "status", "text")
        if status != "recorded":
            raise KnowledgeHttpError("Invalid feedback status response.")
        result = mapped(
            data,
            "feedback receipt",
            [
                (key, "text")
                for key in (
                    "feedbackId",
                    "usageId",
                    "evidenceId",
                    "policyEpoch",
                    "receivedAt",
                    "correlationClass",
                )
            ],
        )
        result.update(
            status=status,
            eligibleForAggregation=checked(
                data.get("eligibleForAggregation"), "eligibleForAggregation", "boolean"
            ),
            evaluatorVersion={
                "uri": checked(evaluator.get("uri"), "evaluatorVersion.uri", "text"),
                "digest": checked(evaluator.get("digest"), "evaluatorVersion.digest", "text"),
            },
        )
        return result


def compatibility_issues(info, config, *, now=None):
    issues = (
        ["version:" + config["expectedVersion"]]
        if config["expectedVersion"] not in info["versions"]
        else []
    )
    issues.extend(
        "operation:" + operation
        for operation in ("query", "receipt", "usage")
        if operation not in info["operations"]
    )
    if not any(
        uri.endswith("/extensions/akep/context-pack/0.1") for uri in info["supportedExtensions"]
    ):
        issues.append("extension:context-pack")
    try:
        expiry = datetime.fromisoformat(info["expiresAt"].replace("Z", "+00:00"))
        if expiry <= (now or datetime.now(UTC)):
            issues.append("capability:expired")
    except (ValueError, TypeError):
        pass
    return issues
