import asyncio
import os

import httpx

from .llm_config import env_flag, project_llm_config

MODEL_IDS = ("local_qwen:qwen3.5-9b-q5km", "deepseek:deepseek-v4-flash", "deepseek-v4-flash")
MESSAGES = {
    "available": "凭据与模型目录检查通过；实际生成能力将在研究运行时验证。",
    "unconfigured": "模型凭据尚未配置。",
    "disabled": "模型已停用。",
    "unauthorized": "模型服务拒绝了当前凭据，请检查凭据及模型访问权限。",
    "unreachable": "无法连接模型服务或服务暂时不可用，请启动服务后重新检查。",
    "unadvertised": "当前凭据可访问的模型目录中没有此模型，请检查模型授权或选择其他模型。",
    "invalid_response": "模型服务返回了无效的模型目录，请检查服务配置。",
}


async def availability(client=None):
    if client is None:
        async with httpx.AsyncClient(timeout=2.5, follow_redirects=False) as owned:
            return await availability(owned)
    modelport = os.getenv(
        "ALPHADECK_DEGRADATION_MODE", ""
    ).strip().lower() != "offline" and env_flag("ALPHADECK_MODELPORT_ENABLED", True)
    catalogs = {}

    async def catalog(config, key):
        try:
            response = await client.get(
                config["baseUrl"].rstrip("/") + "/models",
                headers={"Authorization": "Bearer " + key},
                timeout=2.5,
            )
            if response.status_code in {401, 403}:
                return "unauthorized", []
            if not response.is_success:
                return "unreachable", []
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in data
            ):
                return "invalid_response", []
            return "available", [row["id"] for row in data]
        except (httpx.HTTPError, ValueError):
            return "unreachable", []

    async def check(model):
        config = project_llm_config(model)
        key = os.getenv(config["credentialEnv"], "").strip()
        if not config["agent"]["enabled"] or (model != "deepseek-v4-flash" and not modelport):
            return {
                "id": model,
                "configured": bool(key),
                "status": "disabled",
                "message": "此模型已停用，请选择其他模型或检查运行配置。",
            }
        if not key:
            return {
                "id": model,
                "configured": False,
                "status": "unconfigured",
                "message": f"此模型尚未配置 {config['credentialEnv']}，请配置后重新检查，或选择已就绪的模型。",
            }
        identity = config["baseUrl"] + ":" + config["credentialEnv"]
        if identity not in catalogs:
            catalogs[identity] = asyncio.create_task(catalog(config, key))
        status, ids = await catalogs[identity]
        if (
            status == "available"
            and config["model"] not in ids
            and not (model == "deepseek-v4-flash" and "deepseek-flash" in ids)
        ):
            status = "unadvertised"
        return {"id": model, "configured": True, "status": status, "message": MESSAGES[status]}

    checks = await asyncio.gather(*(check(model) for model in MODEL_IDS))
    ready = [item["id"] for item in checks if item["status"] == "available"]
    return {
        "installed": True,
        "version": "PI Agent Runtime (built-in)",
        "checking": False,
        "configured": any(item["configured"] for item in checks),
        "available": bool(ready),
        "models": ready,
        "modelChecks": checks,
        **({} if ready else {"error": "当前没有通过访问检查的模型，请查看所选模型的状态。"}),
    }
