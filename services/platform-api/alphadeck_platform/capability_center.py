import asyncio

import httpx

from .config import ROOT, market_base_url
from .finance_catalog import FinanceCatalog
from .generation_state import now
from .service_catalog import degradation_config
from .skill_dashboard import active_dashboard
from .strategy_mappers import CATALOG, array, record, string


def readiness(status, missing, errors, endpoints):
    if status == "planned":
        return {
            "status": "planned",
            "score": max(30, 70 - len(missing) * 8 - errors * 6),
            "summary": "规划能力，会降级到已验证执行能力。",
        }
    if missing or errors:
        return {
            "status": "blocked" if missing else "warning",
            "score": max(0, 88 - len(missing) * 14 - errors * 7),
            "summary": f"缺少 {len(missing)} 个依赖 Skill。"
            if missing
            else f"{errors} 个依赖 Skill 健康异常。",
        }
    if not endpoints:
        return {"status": "warning", "score": 78, "summary": "能力可用，但未声明数据端点。"}
    return {"status": "ready", "score": 100, "summary": "依赖 Skill、数据端点和产物契约已声明。"}


def provider(value):
    value = record(value)
    identity, name = string(value.get("id")), string(value.get("name"), string(value.get("id")))
    if not identity or not name:
        return None
    ttl = value.get("cache_ttl_seconds")
    ttl = ttl if type(ttl) in (int, float) else value.get("cacheTtlSeconds")
    return {
        "id": identity,
        "name": name,
        **{
            key: string(value.get(key), default)
            for key, default in (
                ("category", "unknown"),
                ("status", "unknown"),
                ("description", ""),
            )
        },
        "endpoints": [item for item in array(value.get("endpoints")) if isinstance(item, str)],
        "cacheTtlSeconds": ttl if type(ttl) in (int, float) else None,
        "limitations": [item for item in array(value.get("limitations")) if isinstance(item, str)],
    }


async def market_registry(*, client=None):
    fallback = CATALOG["providerFallback"]
    if not degradation_config()["components"]["marketApi"]["enabled"]:
        return {
            "reachable": False,
            "status": "disabled-fallback",
            "error": "market API 已按降级配置停用，展示内置数据源注册表。",
            "providers": fallback,
        }
    owned = client is None
    client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
    try:
        async with asyncio.timeout(2.5):
            base = market_base_url().rstrip("/")
            health = await client.get(base + "/health", timeout=2.5)
            registry = await client.get(base + "/api/v1/registry", timeout=2.5)
            try:
                value = record(registry.json())
            except ValueError:
                value = {}
            providers = [
                item for item in (provider(item) for item in array(value.get("providers"))) if item
            ]
            ok = health.is_success and registry.is_success
            return {
                "reachable": ok,
                "status": "online" if ok else "degraded",
                "error": None
                if ok
                else f"market API returned {health.status_code}/{registry.status_code}",
                "providers": providers or fallback,
            }
    except (TimeoutError, httpx.HTTPError) as cause:
        return {
            "reachable": False,
            "status": "offline",
            "error": str(cause) or "market API timeout",
            "providers": fallback,
        }
    finally:
        if owned:
            await client.aclose()


async def capability_center(*, skills_root=ROOT, client=None):
    degradation = degradation_config()
    skills_data, registry = active_dashboard(skills_root), await market_registry(client=client)
    skills = {item["id"]: item for item in skills_data["skills"]}
    capabilities = []
    catalog = FinanceCatalog()
    for capability in catalog.serialized_capabilities():
        required = [
            {
                **{key: skills[identity][key] for key in ("id", "name", "version", "status")},
                "health": skills[identity]["health"]["status"],
            }
            for identity in capability["requiredSkills"]
            if identity in skills
        ]
        missing = [identity for identity in capability["requiredSkills"] if identity not in skills]
        errors = sum(item["health"] == "error" for item in required)
        capabilities.append(
            {
                **{
                    key: capability[key]
                    for key in (
                        "id",
                        "name",
                        "shortName",
                        "description",
                        "inputHint",
                        "tags",
                        "status",
                        "groupId",
                        "agentType",
                        "executionCapabilityId",
                        "dataEndpoints",
                        "expectedArtifacts",
                        "validationRules",
                    )
                },
                "requiredSkills": required,
                "missingSkills": missing,
                "readiness": readiness(
                    capability["status"], missing, errors, len(capability["dataEndpoints"])
                ),
            }
        )
    summary = {
        "capabilities": len(capabilities),
        "readyCapabilities": sum(item["status"] == "ready" for item in capabilities),
        "plannedCapabilities": sum(item["status"] == "planned" for item in capabilities),
        "blockedCapabilities": sum(
            item["readiness"]["status"] == "blocked" for item in capabilities
        ),
        "skills": skills_data["totals"]["total"],
        "skillErrors": skills_data["totals"]["error"],
        "dataProviders": len(registry["providers"]),
        "availableProviders": sum(item["status"] == "available" for item in registry["providers"]),
        "degradedProviders": sum(item["status"] == "degraded" for item in registry["providers"]),
        "marketApiReachable": registry["reachable"],
    }
    return {
        "generatedAt": now(),
        "defaultCapabilityId": catalog.data["profile"]["defaultCapabilityId"],
        "groups": CATALOG["capabilityGroups"],
        "summary": summary,
        "marketApi": {
            "baseUrl": market_base_url(),
            **{key: registry[key] for key in ("reachable", "status", "error")},
            "checkedAt": now(),
        },
        "degradation": {
            "mode": degradation["mode"],
            "marketApiEnabled": degradation["components"]["marketApi"]["enabled"],
            "marketApiRequired": degradation["components"]["marketApi"]["required"],
        },
        "capabilities": capabilities,
        "dataProviders": registry["providers"],
    }
