import os

import httpx

from .generation_state import now
from .messages import js_length, js_slice
from .skill_source import strict_json
from .strategy_mappers import record

DEFAULTS = {
    "wxwork": "ALPHADECK_WXWORK_RESEARCH_WEBHOOK",
    "feishu": "ALPHADECK_FEISHU_RESEARCH_WEBHOOK",
    "dingtalk": "ALPHADECK_DINGTALK_RESEARCH_WEBHOOK",
    "discord": "ALPHADECK_DISCORD_RESEARCH_WEBHOOK",
}


def text(value, fallback=""):
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def compact(value, maximum):
    return js_slice(value, 0, maximum - 1) + "…" if js_length(value) > maximum else value


def resolve_webhook(channel, env=None):
    env, config = os.environ if env is None else env, record(channel.get("config"))
    explicit = text(config.get("webhookUrl")) or text(config.get("webhook_url"))
    if explicit:
        return {"url": explicit, "source": "channel.config.webhookUrl"}
    name = (
        text(config.get("webhookEnv"))
        or text(config.get("webhook_env"))
        or DEFAULTS.get(channel["channelType"])
    )
    return (
        {"url": env.get(name, "").strip(), "source": name}
        if name
        else {"url": "", "source": "unsupported"}
    )


def webhook_payload(channel, report):
    markdown = compact(report["contentMarkdown"], 3600)
    plain = "\n".join(
        [
            report["title"],
            "",
            report["summary"],
            "",
            f"评分：{report['score']}/100",
            "风险：" + report["riskLevel"],
            "建议：" + report["recommendation"],
        ]
    )
    plain = compact(plain, 1800)
    kind = channel["channelType"]
    if kind == "wxwork":
        return {
            "body": {"msgtype": "markdown", "markdown": {"content": markdown}},
            "preview": markdown,
        }
    if kind == "feishu":
        return {"body": {"msg_type": "text", "content": {"text": plain}}, "preview": plain}
    if kind == "dingtalk":
        return {
            "body": {
                "msgtype": "markdown",
                "markdown": {"title": report["title"], "text": markdown},
            },
            "preview": markdown,
        }
    if kind == "discord":
        return {"body": {"content": compact(plain, 1900)}, "preview": plain}
    return None


def webhook_failure(kind, body):
    body = record(body)
    error, code = body.get("errcode"), body.get("code", body.get("StatusCode"))
    if kind in ("wxwork", "dingtalk") and type(error) in (int, float) and error != 0:
        return text(body.get("errmsg"), f"webhook errcode={error}")
    if kind == "feishu" and type(code) in (int, float) and code != 0:
        return text(body.get("msg", body.get("StatusMessage")), f"webhook code={code}")
    return None


async def post_webhook(channel, url, payload, *, client=None):
    owned = client is None
    client = client or httpx.AsyncClient(follow_redirects=False)
    try:
        response = await client.post(url, json=payload["body"], timeout=8)
        body, parsed = response.text, None
        if body.strip():
            try:
                parsed = strict_json(body)
            except ValueError:
                parsed = {"raw": js_slice(body, 0, 500)}
        if not response.is_success:
            return {
                "ok": False,
                "error": f"HTTP {response.status_code}: " + js_slice(body, 0, 240),
                "responseJson": parsed,
            }
        error = webhook_failure(channel["channelType"], parsed)
        return {"ok": error is None, "error": error, "responseJson": parsed}
    except httpx.HTTPError as cause:
        return {"ok": False, "error": str(cause), "responseJson": None}
    finally:
        if owned:
            await client.aclose()


async def deliver_notification(channel, report, *, force_dry_run=False, client=None, env=None):
    payload, webhook = webhook_payload(channel, report), resolve_webhook(channel, env)
    kind = channel["channelType"]
    common = {"channelType": kind, "title": report["title"]}
    if payload is None:
        return {
            **common,
            "status": "skipped",
            "payload": {
                "adapter": "unsupported",
                "channelId": channel["id"],
                "channelName": channel["name"],
            },
            "error": f"暂不支持 {kind} 推送 adapter",
            "deliveredAt": None,
        }
    metadata = {
        "adapter": kind + "-webhook",
        "mode": "real",
        "channelId": channel["id"],
        "channelName": channel["name"],
        "target": channel.get("target"),
        "webhookSource": webhook["source"],
        "webhookConfigured": bool(webhook["url"]),
        "reportId": report["id"],
    }
    if force_dry_run or channel["isDryRun"]:
        return {
            **common,
            "status": "dry_run",
            "payload": {
                **metadata,
                "mode": "dry_run",
                "preview": compact(payload["preview"], 1200),
                "score": report["score"],
                "riskLevel": report["riskLevel"],
            },
            "error": None,
            "deliveredAt": now(),
        }
    if not webhook["url"]:
        return {
            **common,
            "status": "failed",
            "payload": metadata,
            "error": f"缺少 {kind} webhook，配置 {webhook['source']} 后再发送",
            "deliveredAt": None,
        }
    result = await post_webhook(channel, webhook["url"], payload, client=client)
    return {
        **common,
        "status": "delivered" if result["ok"] else "failed",
        "payload": {
            **metadata,
            "score": report["score"],
            "riskLevel": report["riskLevel"],
            "response": result["responseJson"],
        },
        "error": result["error"],
        "deliveredAt": now() if result["ok"] else None,
    }
