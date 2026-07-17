import hashlib
import os
import re
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .messages import js_length
from .provenance import js_json


def flag(value, fallback):
    value = (value or "").strip().lower()
    return (
        True
        if value in {"1", "true", "yes", "on", "enabled"}
        else False
        if value in {"0", "false", "no", "off", "disabled"}
        else fallback
    )


def bounded(value, fallback, minimum, maximum):
    match = re.match(r"^[+-]?[0-9]+", (value or "").lstrip())
    parsed = int(match[0]) if match else fallback
    return parsed if minimum <= parsed <= maximum else fallback


def http_url(value, label):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        normalized = str(httpx.URL(value))
        if not parsed.path:
            normalized = normalized.rstrip("/") + "/"
        return normalized[:-1] if normalized.endswith("/") else normalized
    except (ValueError, httpx.InvalidURL) as cause:
        raise ValueError(label) from cause


def memory_config(environment=None):
    env = os.environ if environment is None else environment
    enabled = env.get("ALPHADECK_DEGRADATION_MODE", "").strip().lower() != "offline" and flag(
        env.get("ALPHADECK_MEMORY_ENABLED"), env.get("NODE_ENV") != "test"
    )
    production = env.get("NODE_ENV") == "production"
    bearer = env.get("ALPHADECK_MEMORY_BEARER_TOKEN", "").strip() or None
    if production and bearer:
        raise ValueError("Static personal memory bearer tokens are forbidden in production.")
    tenant = env.get("ALPHADECK_MEMORY_TENANT_ID", "").strip() or "alphadeck-local"
    if js_length(tenant) > 128:
        raise ValueError("ALPHADECK_MEMORY_TENANT_ID must be at most 128 characters.")
    api = http_url(
        env.get("ALPHADECK_MEMORY_API_URL", "").strip() or "http://127.0.0.1:38089",
        "ALPHADECK_MEMORY_API_URL must be an HTTP(S) base URL without credentials.",
    )
    url, client, secret = [
        env.get("ALPHADECK_MEMORY_TOKEN_BROKER_" + key, "").strip()
        for key in ("URL", "CLIENT_ID", "CLIENT_SECRET")
    ]
    audience = env.get("ALPHADECK_MEMORY_TOKEN_AUDIENCE", "").strip() or "evolvable-memory-api"
    broker = None
    if not (url or client or secret):
        if enabled and production:
            raise ValueError("Production personal memory requires a scoped token broker.")
    else:
        if not (url and client and secret):
            raise ValueError(
                "Personal memory token broker URL, client ID and client secret are all required."
            )
        url = http_url(
            url, "ALPHADECK_MEMORY_API_URL must be an HTTP(S) base URL without credentials."
        )
        if production and not url.startswith("https://"):
            raise ValueError("Production personal memory token broker must use HTTPS.")
        if js_length(client) > 256 or ":" in client or not 16 <= js_length(secret) <= 4096:
            raise ValueError("Invalid personal memory token broker credentials.")
        if not audience or js_length(audience) > 512:
            raise ValueError("Invalid personal memory token audience.")
        broker = {"url": url, "clientId": client, "clientSecret": secret, "audience": audience}
    return {
        "enabled": enabled,
        "required": enabled and flag(env.get("ALPHADECK_MEMORY_REQUIRED"), False),
        "requireProductionReady": enabled
        and flag(env.get("ALPHADECK_MEMORY_REQUIRE_PRODUCTION_READY"), production),
        "apiUrl": api,
        "tenantId": tenant,
        "purpose": "personalization",
        "timeoutMs": bounded(env.get("ALPHADECK_MEMORY_TIMEOUT_MS"), 5000, 100, 30000),
        "recallLimit": bounded(env.get("ALPHADECK_MEMORY_RECALL_LIMIT"), 6, 1, 100),
        "maxProjectionCharacters": bounded(
            env.get("ALPHADECK_MEMORY_MAX_CONTEXT_CHARACTERS"), 2000, 64, 100000
        ),
        "bearerToken": bearer,
        "tokenBroker": broker,
        "expectedContract": "evolvable-memory-http/v1",
    }


def knowledge_config(environment=None):
    env = os.environ if environment is None else environment
    enabled = env.get("ALPHADECK_DEGRADATION_MODE", "").strip().lower() != "offline" and flag(
        env.get("ALPHADECK_KNOWLEDGE_ENABLED"), env.get("NODE_ENV") != "test"
    )
    production = env.get("NODE_ENV") == "production"
    api = http_url(
        env.get("ALPHADECK_KNOWLEDGE_API_URL", "").strip() or "http://localhost:33005",
        "ALPHADECK_KNOWLEDGE_API_URL must be an HTTP(S) URL without credentials, query, or fragment.",
    )
    if production and not api.startswith("https://"):
        raise ValueError("Production governed knowledge endpoint must use HTTPS.")
    bearer = env.get("ALPHADECK_KNOWLEDGE_BEARER_TOKEN", "").strip() or None
    if production and bearer:
        raise ValueError("Static governed knowledge bearer tokens are forbidden in production.")
    purpose = env.get("ALPHADECK_KNOWLEDGE_PURPOSE", "").strip() or "quant-research"
    if not re.fullmatch(r"[a-z][a-z0-9._:-]{0,127}", purpose):
        raise ValueError("ALPHADECK_KNOWLEDGE_PURPOSE is invalid.")
    spaces = [
        value.strip()
        for value in (
            env.get("ALPHADECK_KNOWLEDGE_SPACES", "").strip()
            or "https://knowledge.local/spaces/default"
        ).split(",")
        if value.strip()
    ]
    if not 1 <= len(spaces) <= 50:
        raise ValueError("ALPHADECK_KNOWLEDGE_SPACES must contain between 1 and 50 URI values.")
    for space in spaces:
        parsed = urlsplit(space)
        if not parsed.scheme or parsed.username or parsed.password:
            raise ValueError("ALPHADECK_KNOWLEDGE_SPACES contains an invalid URI.")
    base = (
        env.get("ALPHADECK_KNOWLEDGE_PROJECT_SPACE_BASE_URL", "").strip()
        or "https://knowledge.local/spaces/alphadeck/projects"
    )
    parsed = urlsplit(base)
    if not parsed.scheme or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("ALPHADECK_KNOWLEDGE_PROJECT_SPACE_BASE_URL contains an invalid URI.")
    base = base[:-1] if base.endswith("/") else base
    url, client, secret = [
        env.get("ALPHADECK_KNOWLEDGE_OAUTH_" + key, "").strip()
        for key in ("TOKEN_URL", "CLIENT_ID", "CLIENT_SECRET")
    ]
    oauth = None
    if not (url or client or secret):
        if enabled and production:
            raise ValueError("Production governed knowledge requires OAuth client credentials.")
    else:
        if not (url and client and secret):
            raise ValueError(
                "Knowledge OAuth token URL, client ID, and client secret are all required."
            )
        url = http_url(
            url,
            "ALPHADECK_KNOWLEDGE_OAUTH_TOKEN_URL must be an HTTP(S) URL without credentials, query, or fragment.",
        )
        if production and not url.startswith("https://"):
            raise ValueError("Production knowledge OAuth token endpoint must use HTTPS.")
        if js_length(client) > 256 or ":" in client or not 16 <= js_length(secret) <= 4096:
            raise ValueError("Invalid knowledge OAuth client credentials.")
        api_parts = urlsplit(api)
        resource = env.get("ALPHADECK_KNOWLEDGE_OAUTH_RESOURCE", "").strip() or urlunsplit(
            (api_parts.scheme, api_parts.netloc, "/akep/0.1", "", "")
        )
        scope = (
            env.get("ALPHADECK_KNOWLEDGE_OAUTH_SCOPE", "").strip()
            or "akep:query akep:read akep:feedback"
        )
        if js_length(resource) > 2048 or js_length(scope) > 2048:
            raise ValueError("Knowledge OAuth resource or scope is too long.")
        oauth = {
            "tokenUrl": url,
            "clientId": client,
            "clientSecret": secret,
            "resource": resource,
            "scope": scope,
        }
    return {
        "enabled": enabled,
        "required": enabled and flag(env.get("ALPHADECK_KNOWLEDGE_REQUIRED"), False),
        "apiUrl": api,
        "purpose": purpose,
        "spaces": list(dict.fromkeys(spaces)),
        "projectSpacesEnabled": flag(env.get("ALPHADECK_KNOWLEDGE_PROJECT_SPACES_ENABLED"), True),
        "projectSpaceBaseUrl": base,
        "timeoutMs": bounded(env.get("ALPHADECK_KNOWLEDGE_TIMEOUT_MS"), 2000, 100, 30000),
        "maxContextCharacters": bounded(
            env.get("ALPHADECK_KNOWLEDGE_MAX_CONTEXT_CHARACTERS"), 8000, 256, 100000
        ),
        "supportedObligations": ["cite", "no-train"],
        "bearerToken": bearer,
        "oauth": oauth,
        "expectedVersion": "0.1",
    }


def scope_id(value, fallback, label):
    value = (value or "").strip() or fallback
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError(label + " must contain 1-128 safe scope characters.")
    return value


def create_integration_scope(project, memory, knowledge, environment=None):
    env = os.environ if environment is None else environment
    project = project.strip()
    if not project or js_length(project) > 160 or re.search(r"[\x00-\x1f\x7f]", project):
        raise ValueError("Project integration scope project ID is invalid.")
    consumer = scope_id(
        env.get("ALPHADECK_INTEGRATION_CONSUMER_ID"),
        "alphadeck",
        "ALPHADECK_INTEGRATION_CONSUMER_ID",
    )
    tenant = scope_id(memory["tenantId"], "alphadeck-local", "Memory tenant ID")
    project_space = (
        knowledge["projectSpaceBaseUrl"].rstrip("/") + "/" + quote(project, safe="~!*'()-._")
        if knowledge["projectSpacesEnabled"]
        else None
    )
    shared = sorted(
        set(knowledge["spaces"]), key=lambda value: value.encode("utf-16-be", "surrogatepass")
    )
    requested = sorted(
        set([*shared, *([project_space] if project_space else [])]),
        key=lambda value: value.encode("utf-16-be", "surrogatepass"),
    )
    unsigned = {
        "schemaVersion": 1,
        "consumerId": consumer,
        "projectId": project,
        "modelPort": {
            "organizationId": scope_id(
                env.get("ALPHADECK_MODELPORT_ORGANIZATION_ID"),
                "org_local",
                "ALPHADECK_MODELPORT_ORGANIZATION_ID",
            ),
            "projectId": scope_id(
                env.get("ALPHADECK_MODELPORT_PROJECT_ID"),
                "prj_alphadeck",
                "ALPHADECK_MODELPORT_PROJECT_ID",
            ),
            "environmentId": scope_id(
                env.get("ALPHADECK_MODELPORT_ENVIRONMENT_ID"),
                "env_production" if env.get("NODE_ENV") == "production" else "env_development",
                "ALPHADECK_MODELPORT_ENVIRONMENT_ID",
            ),
        },
        "memory": {"tenantId": tenant, "context": {"product": "alphadeck", "project_id": project}},
        "knowledge": {
            "sharedSpaceIds": shared,
            "projectSpaceId": project_space,
            "requestedSpaceIds": requested,
        },
    }
    return {
        **unsigned,
        "scopeSha256": "sha256:" + hashlib.sha256(js_json(unsigned).encode()).hexdigest(),
    }


def integration_scope(project, environment=None):
    return create_integration_scope(
        project, memory_config(environment), knowledge_config(environment), environment
    )


def modelport_scope_headers(scope):
    return {
        "X-ModelPort-Organization-Id": scope["modelPort"]["organizationId"],
        "X-ModelPort-Project-Id": scope["modelPort"]["projectId"],
        "X-ModelPort-Environment-Id": scope["modelPort"]["environmentId"],
    }
