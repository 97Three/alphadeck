import json
import os
import re
from urllib.parse import urljoin, urlsplit

from .config import ROOT
from .integration_config import flag


def document():
    return json.loads((ROOT / "config/service-catalog.json").read_text())


def mask_endpoint(value):
    value = re.sub(r"://([^:/?#]+):([^@/?#]+)@", r"://\1:***@", value)
    return re.sub(r"([?&](?:password|token|key|secret)=)[^&]+", r"\1***", value, flags=re.I)


def endpoint(entry, env):
    configured = next(
        ((env[key].strip(), key) for key in entry["endpoint"]["env"] if env.get(key, "").strip()),
        None,
    )
    raw = configured[0] if configured else entry["endpoint"].get("default")
    return (
        raw,
        "env" if configured else "default" if raw else "missing",
        configured[1] if configured else None,
    )


def health_url(raw, definition):
    if not raw or definition["protocol"] != "http" or not definition.get("healthPath"):
        return None

    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            return None
        return urljoin(raw, definition["healthPath"])
    except ValueError:
        return None


def raw_endpoint(identity, env=None):
    entry = next((item for item in document()["services"] if item["id"] == identity), None)
    return endpoint(entry, os.environ if env is None else env)[0] if entry else None


def resolved_catalog(env=None, catalog=None):
    env = os.environ if env is None else env
    result = []
    for entry in (catalog or document())["services"]:
        enabled = flag(
            env.get(entry["enabledEnv"]) if entry.get("enabledEnv") else None,
            entry["enabledByDefault"],
        )
        required = enabled and flag(
            env.get(entry["requiredEnv"]) if entry.get("requiredEnv") else None,
            entry["requiredByDefault"],
        )
        raw, source, key = endpoint(entry, env)
        health = health_url(raw, entry["endpoint"])
        issues = [entry["name"] + " 缺少 endpoint 配置。"] if enabled and not raw else []
        result.append(
            {
                **entry,
                "enabled": enabled,
                "required": required,
                "requirement": "disabled"
                if not enabled
                else "required"
                if required
                else "optional",
                "endpoint": mask_endpoint(raw) if raw else None,
                "endpointSource": source,
                "endpointEnvKey": key,
                "endpointProtocol": entry["endpoint"]["protocol"],
                "healthUrl": mask_endpoint(health) if health else None,
                "configurationStatus": "disabled"
                if not enabled
                else "failed"
                if required and issues
                else "warning"
                if issues
                else "ok",
                "issues": issues,
            }
        )
    return result


def dependency_edges(services):
    by_id = {entry["id"]: entry for entry in services}
    return [
        {
            "from": entry["id"],
            "fromName": entry["name"],
            "to": dependency,
            "toName": by_id.get(dependency, {}).get("name", dependency),
            "active": bool(entry["enabled"] and by_id.get(dependency, {}).get("enabled")),
        }
        for entry in services
        for dependency in entry["dependencies"]
    ]


def validate_catalog(env=None, catalog=None):
    catalog = catalog or document()
    services, errors, warnings, seen = catalog["services"], [], [], set()
    by_id = {entry["id"]: entry for entry in services}
    for entry in services:
        identity = entry["id"]
        if not re.fullmatch(r"[a-z][a-z0-9-]*", identity):
            errors.append("服务 id 不合法：" + identity)
        if identity in seen:
            errors.append("服务 id 重复：" + identity)
        seen.add(identity)
        spec = entry["endpoint"]
        if not spec["env"] and not spec.get("default"):
            errors.append(identity + " 缺少 endpoint.env 或 endpoint.default。")
        if (
            spec["protocol"] == "http"
            and spec.get("healthPath")
            and not spec["healthPath"].startswith("/")
        ):
            errors.append(identity + " 的 healthPath 必须以 / 开头。")
        for key in [entry.get("enabledEnv"), entry.get("requiredEnv"), *spec["env"]]:
            if key and not re.fullmatch(r"[A-Z0-9_]+", key):
                errors.append(identity + " 使用了不规范的环境变量名：" + key)
        for dependency in entry["dependencies"]:
            if dependency not in by_id:
                errors.append(identity + " 依赖不存在的服务：" + dependency)
    visiting, visited = set(), set()

    def walk(identity, stack):
        if identity in visiting:
            errors.append(
                "服务依赖存在环：" + " -> ".join(stack[stack.index(identity) :] + [identity])
            )
            return
        if identity in visited or identity not in by_id:
            return
        visiting.add(identity)
        for dependency in by_id[identity]["dependencies"]:
            walk(dependency, [*stack, dependency])
        visiting.remove(identity)
        visited.add(identity)

    for entry in services:
        walk(entry["id"], [entry["id"]])
    resolved = resolved_catalog(env, catalog)
    for entry in resolved:
        target = (
            errors
            if entry["configurationStatus"] == "failed"
            else warnings
            if entry["configurationStatus"] == "warning"
            else None
        )
        if target is not None:
            target.extend(entry["id"] + ": " + issue for issue in entry["issues"])
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "serviceCount": len(resolved),
        "enabledCount": sum(entry["enabled"] for entry in resolved),
        "requiredCount": sum(entry["required"] for entry in resolved),
        "dependencyCount": len(dependency_edges(resolved)),
    }


def degradation_config(env=None):
    env = os.environ if env is None else env
    mode = env.get("ALPHADECK_DEGRADATION_MODE", "").strip().lower()
    mode = mode if mode in ("offline", "strict") else "auto"
    offline, strict = mode == "offline", mode == "strict"
    components = {}
    for name, key, default, required in [
        ("database", "DATABASE", True, True),
        ("knowledge", "KNOWLEDGE", env.get("NODE_ENV") != "test", False),
        ("marketApi", "MARKET_API", True, strict),
        ("memory", "MEMORY", env.get("NODE_ENV") != "test", False),
        ("modelPort", "MODELPORT", True, strict),
        ("observability", "OBSERVABILITY", True, strict),
        ("redis", "REDIS", True, strict),
    ]:
        enabled_key = (
            "ALPHADECK_REDIS_CACHE_ENABLED"
            if name == "redis"
            else "ALPHADECK_" + key + "_ENABLED"
        )
        components[name] = {
            "enabled": False
            if offline and name != "database"
            else flag(env.get(enabled_key), default),
            "required": not offline and flag(env.get("ALPHADECK_" + key + "_REQUIRED"), required),
        }
    return {"mode": mode, "components": components}
