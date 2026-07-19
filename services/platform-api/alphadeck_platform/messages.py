import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .quota import one
from .sessions import iso


def js_length(value):
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def js_slice(value, start, end=None):
    encoded = value.encode("utf-16-le", errors="surrogatepass")
    return encoded[slice(start * 2, end * 2 if end is not None else None)].decode(
        "utf-16-le", errors="surrogatepass"
    )


class MessageResponse(JSONResponse):
    def render(self, content):
        # JSON.stringify escapes lone UTF-16 surrogates produced by String.slice.
        return json.dumps(
            content, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode()


def compact_output(value, limit=12000):
    size = js_length(value)
    if size <= limit:
        return value
    if limit <= 0:
        return ""

    def marker(omitted):
        return f"\n\n[AlphaDeck 已截断 {omitted} 个字符；原始输出 {size} 个字符，以下保留末尾诊断信息。]\n\n"

    note = marker(size)
    preserved = max(0, limit - js_length(note))
    for _ in range(4):
        note = marker(size - preserved)
        next_preserved = max(0, limit - js_length(note))
        if next_preserved == preserved:
            break
        preserved = next_preserved
    if js_length(note) >= limit:
        return js_slice(note, 0, limit)
    tail = min(preserved // 3, 4000)
    return js_slice(value, 0, preserved - tail) + note + (js_slice(value, -tail) if tail else "")


def serialize_message(row):
    try:
        metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else None
    except (ValueError, TypeError):
        metadata = None
    if isinstance(metadata, dict):
        original = metadata.get("toolOutputOriginalChars", 0)
        if type(original) not in {int, float}:
            original = 0
        for key in (
            "toolOutput",
            "tool_output",
            "output",
            "result",
            "content",
            "diff",
            "diffInfo",
            "diff_info",
        ):
            value = metadata.get(key)
            if isinstance(value, str):
                metadata[key] = compact_output(value)
                if metadata[key] != value:
                    original = max(original, js_length(value))
        if original > 0:
            metadata.update(toolOutputTruncated=True, toolOutputOriginalChars=original)
    content = (
        compact_output(row["content"]) if row["message_type"] == "tool_result" else row["content"]
    )
    if content != row["content"]:
        metadata = {
            **(metadata if isinstance(metadata, dict) else {}),
            "toolOutputTruncated": True,
            "toolOutputOriginalChars": max(
                js_length(row["content"]), (metadata or {}).get("toolOutputOriginalChars", 0)
            ),
        }
    return {
        "id": row["id"],
        "projectId": row["project_id"],
        "role": row["role"],
        "messageType": row["message_type"],
        "content": content,
        "metadata": metadata,
        "parentMessageId": row["parent_message_id"],
        "conversationId": row["conversation_id"],
        "cliSource": row["cli_source"],
        **({"requestId": row["request_id"]} if row["request_id"] else {}),
        "createdAt": iso(row["created_at"]),
        "updatedAt": iso(row.get("updated_at") or row["created_at"]),
    }


async def insert_message(
    connection,
    project_id,
    role,
    content,
    *,
    message_type="chat",
    metadata=None,
    conversation=None,
    request_id=None,
    identity=None,
    cli="pi",
    ensure=False,
):
    identity = identity or str(uuid4())
    row = await one(
        connection,
        """INSERT INTO messages (id,project_id,role,message_type,content,metadata_json,conversation_id,request_id,cli_source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"""
        + (" ON CONFLICT (id) DO NOTHING" if ensure else "")
        + " RETURNING *",
        (
            identity,
            project_id,
            role,
            message_type,
            content,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) if metadata else None,
            conversation,
            request_id,
            cli,
        ),
    )
    if not row:
        row = await one(connection, "SELECT * FROM messages WHERE id=%s", (identity,))
        if (row["project_id"], row["role"], row["message_type"], row["request_id"]) != (
            project_id,
            role,
            message_type,
            request_id,
        ):
            raise ValueError("Stable message ID collision")
    return row


def parsed_integer(value, default, minimum=0, maximum=None):
    match = re.match(r"^[\s]*([+-]?\d+)", value or "")
    result = max(minimum, int(match[1])) if match else default
    return min(maximum, result) if maximum else result


def message_routes(services):
    router = APIRouter()

    @router.get("/api/chat/{project_id}/messages")
    async def history(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.read", project_id)
        limit = parsed_integer(request.query_params.get("limit"), 50, 1, 500)
        offset = parsed_integer(request.query_params.get("offset"), 0)
        after = request.query_params.get("after")
        after_id = request.query_params.get("afterId")
        parameters = (project_id,)
        where = "project_id=%s"
        if after:
            try:
                timestamp = datetime.fromisoformat(after.replace("Z", "+00:00"))
                timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
            except ValueError:
                return JSONResponse(
                    {"success": False, "error": "Invalid after cursor"}, status_code=400
                )
            where += (
                " AND (created_at>%s OR (created_at=%s AND id>%s))"
                if after_id
                else " AND created_at>%s"
            )
            parameters += (timestamp, timestamp, after_id) if after_id else (timestamp,)
        async with database.transaction() as connection:
            total = (
                await one(
                    connection,
                    "SELECT count(*) AS n FROM messages WHERE project_id=%s",
                    (project_id,),
                )
            )["n"]
            rows = await (
                await connection.execute(
                    f"SELECT * FROM messages WHERE {where} ORDER BY created_at,id LIMIT %s OFFSET %s",
                    parameters + (limit, 0 if after else offset),
                )
            ).fetchall()
        data = [serialize_message(row) for row in rows]
        pagination = {
            "limit": limit,
            "offset": offset,
            "count": len(data),
            "hasMore": offset + len(data) < total,
        }
        if after:
            latest = data[-1] if data else None
            pagination = {
                "mode": "incremental",
                "limit": limit,
                "offset": None,
                "count": len(data),
                "hasMore": len(data) == limit,
                "cursor": {
                    "id": latest["id"] if latest else after_id,
                    "createdAt": latest["createdAt"] if latest else after,
                },
            }
        return MessageResponse(
            {"success": True, "data": data, "totalCount": total, "pagination": pagination},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/chat/{project_id}/messages")
    async def create(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.update", project_id)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.keys() - {
                "content",
                "role",
                "messageType",
                "conversationId",
                "cliSource",
            }:
                raise ValueError("Invalid message fields")
            content, role = body.get("content"), body.get("role", "user")
            kind = body.get("messageType", "info" if role == "system" else "chat")
            if (
                not isinstance(content, str)
                or not 1 <= js_length(content.strip()) <= 200000
                or role not in {"assistant", "user", "system", "tool"}
                or kind not in {"chat", "tool_use", "tool_result", "error", "info", "system"}
                or ("cliSource" in body and body["cliSource"] != "pi")
            ):
                raise ValueError("Invalid message request")
            conversation = body.get("conversationId")
            if "conversationId" in body and (
                not isinstance(conversation, str) or not 1 <= js_length(conversation.strip()) <= 256
            ):
                raise ValueError("Invalid conversation ID")
        except ValueError as error:
            return JSONResponse(
                {
                    "success": False,
                    "error": "INVALID_MESSAGE_REQUEST",
                    "issues": [{"code": "custom", "path": [], "message": str(error)}],
                },
                status_code=400,
            )
        async with database.transaction() as connection:
            row = await insert_message(
                connection,
                project_id,
                role,
                content.strip(),
                message_type=kind,
                conversation=conversation.strip() if conversation else None,
                cli=body.get("cliSource"),
            )
        return MessageResponse(
            {"success": True, "data": serialize_message(row)},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @router.delete("/api/chat/{project_id}/messages")
    async def remove(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.update", project_id)
        conversation = request.query_params.get("conversationId")
        async with database.transaction() as connection:
            result = await connection.execute(
                "DELETE FROM messages WHERE project_id=%s"
                + (" AND conversation_id=%s" if conversation else ""),
                (project_id, conversation) if conversation else (project_id,),
            )
        return {"success": True, "deleted": result.rowcount}

    return router
