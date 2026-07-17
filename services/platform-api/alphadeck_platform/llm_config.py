import json
import os
import re
from copy import deepcopy

from .config import ROOT
from .settings import normalize_model


def env_flag(name, fallback):
    value = os.getenv(name, "").strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    return fallback


def bounded_integer(name, fallback, minimum, maximum):
    match = re.match(r"^[+-]?[0-9]+", os.getenv(name, "").lstrip())
    value = int(match[0]) if match else fallback
    return value if minimum <= value <= maximum else fallback


def project_llm_config(model=None):
    catalog = json.loads((ROOT / "config/llm.json").read_text())
    model = normalize_model(model or catalog["defaultProfileId"])
    profile = deepcopy(catalog["profiles"][model])
    expected = (
        ("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY")
        if model == "deepseek-v4-flash"
        else ("openai", "http://127.0.0.1:38082/v1", "MODELPORT_API_KEY")
    )
    if (profile["provider"], profile["baseUrl"], profile["credentialEnv"]) != expected or profile[
        "model"
    ] != model:
        raise ValueError("LLM profile must use the locked provider boundary")
    rewrite = profile.get("queryRewrite", {})
    return {
        "schemaVersion": 1,
        "profileId": model,
        "provider": profile["provider"],
        "model": model,
        "baseUrl": profile["baseUrl"],
        "credentialEnv": profile["credentialEnv"],
        "agent": {
            "enabled": env_flag(
                "ALPHADECK_LLM_AGENT_ENABLED", profile.get("agent", {}).get("enabled", True)
            )
        },
        "queryRewrite": {
            "enabled": env_flag(
                "ALPHADECK_LLM_QUERY_REWRITE_ENABLED", rewrite.get("enabled", True)
            ),
            "timeoutMs": bounded_integer(
                "ALPHADECK_QUERY_REWRITE_LLM_TIMEOUT_MS",
                rewrite.get("timeoutMs", 4000),
                500,
                15000,
            ),
            "maxRetries": bounded_integer(
                "ALPHADECK_QUERY_REWRITE_LLM_MAX_RETRIES", rewrite.get("maxRetries", 0), 0, 1
            ),
        },
    }
