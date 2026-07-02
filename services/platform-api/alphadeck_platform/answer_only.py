"""A bounded text answer path that never starts dashboard generation."""

import asyncio
import logging

from .agent_executor import AgentExecutor
from .llm_config import project_llm_config
from .messages import insert_message, serialize_message
from .realtime import Realtime
from .user_requests import UserRequests

SYSTEM = (
    "你是 AlphaDeck 的研究问答助手。直接回答用户的问题，不生成网页、看板或代码。"
    "你当前没有实时行情查询工具；若问题需要最新价格或可核验的数值，明确说明无法核验，"
    "不要编造行情、来源、收益或引用。用简洁中文回答。"
)


async def complete_text(provider, model, history):
    parts = []
    async with asyncio.timeout(90):
        async for event in provider.complete(
            {"model": model, "messages": [{"role": "system", "content": SYSTEM}, *history], "maxTokens": 1600}
        ):
            if event["type"] == "text_delta":
                parts.append(event["delta"])
                if sum(map(len, parts)) > 16000:
                    raise ValueError("Model answer exceeded the response limit")
    result = "".join(parts).strip()
    if not result:
        raise ValueError("Model returned an empty answer")
    return result


async def answer_only(database, projects, project_id, request_id, selected_model, conversation_id):
    requests = UserRequests(database)
    await requests.status(project_id, request_id, "processing")
    rows = await database.fetch_all(
        """SELECT role,content FROM messages WHERE project_id=%s AND message_type='chat'
        AND role IN ('user','assistant') ORDER BY created_at DESC,id DESC LIMIT 12""",
        (project_id,),
    )
    history = [{"role": row["role"], "content": row["content"][:12000]} for row in reversed(rows)]
    config = project_llm_config(selected_model)
    provider = AgentExecutor(projects).provider(config)
    try:
        content = await complete_text(provider, config["model"], history)
        async with database.transaction() as tx:
            message = await insert_message(
                tx, project_id, "assistant", content,
                request_id=request_id, conversation=conversation_id,
                metadata={"answerOnly": True},
            )
        await requests.status(project_id, request_id, "completed")
        try:
            await Realtime(database).publish(project_id, {"type": "message", "data": serialize_message(message)})
        except Exception:
            logging.warning("Answer was saved but could not be projected for %s", project_id)
        return message
    except Exception:
        await requests.status(project_id, request_id, "failed", "问答模型调用失败，请检查模型连接后重试。")
        raise
