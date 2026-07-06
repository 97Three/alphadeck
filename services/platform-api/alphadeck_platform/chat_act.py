import json
import logging
from contextlib import suppress
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .answer_only import answer_only
from .api_errors import ApiError
from .attachment_context import prepare_attachments
from .chat_contract import ingress, parse_act
from .dispatch import Dispatch
from .finance_catalog import FinanceCatalog
from .finance_request import parse_preparation
from .generation_envelope import create_finance_envelope
from .http_input import bounded_json
from .memory_policy import detect_candidate
from .messages import insert_message, serialize_message
from .projects import Projects, serialize_project
from .quota import Quota
from .realtime import Realtime
from .settings import normalize_model
from .user_requests import RequestBindingError, UserRequests


def act_routes(services, root):
    router = APIRouter()

    @router.post("/api/chat/{project_id}/act")
    async def act(request: Request, project_id: str):
        database, auth = services()
        context = await auth.require_action(request, "agent.run", project_id)
        body = parse_act(await bounded_json(request, 27 * 1048576))
        if body.get("mode") == "chat" and body["images"]:
            raise ApiError(400, "ANSWER_ONLY_IMAGES_UNSUPPORTED", "只做问答暂不支持图片，请选择生成看板来分析图片。")
        identity = body.get("requestId") or uuid4().hex
        ingress(body["instruction"], body.get("displayInstruction"), identity)
        projects, requests = Projects(database, root), UserRequests(database)
        project = await projects.get(project_id)
        path = projects.managed_path(project, must_exist=True)
        actor = context.user["id"] if context.user else None
        try:
            await requests.create(
                identity,
                project_id,
                body.get("displayInstruction", "").strip()
                or body["instruction"].strip()
                or "请分析用户上传的图片附件。",
                actor=actor,
                cli="pi",
            )
        except RequestBindingError as cause:
            return JSONResponse(
                {
                    "success": False,
                    "error": "REQUEST_ID_ALREADY_EXISTS"
                    if cause.code == "REQUEST_ALREADY_EXISTS"
                    else "Request ID belongs to a different project or user",
                    "message": "该 requestId 已被使用；请读取原请求状态，或为新的执行生成新 requestId。",
                    "requestId": identity,
                },
                status_code=409,
            )
        try:
            images, attachment_path, image_instruction = await prepare_attachments(
                path, project_id, identity, body["images"]
            )
            final = "\n\n".join(
                filter(
                    None,
                    [
                        body["instruction"].strip()
                        or ("请分析用户上传的图片附件。" if images else ""),
                        image_instruction,
                    ],
                )
            ).strip()
            display = body.get("displayInstruction", body["instruction"]).strip() or (
                "请分析上传的图片附件" if images else final
            )
            if not final:
                # A display-only request is a valid contract input; preserve it for execution.
                final = display
            if actor:
                await Quota(database).consume(
                    actor,
                    "agent.requests.daily",
                    1,
                    f"agent-request:{actor}:{identity}",
                    "user_request",
                    project_id=project_id,
                    source_id=identity,
                    event_key=f"user-request:{identity}:daily",
                )
            candidate = detect_candidate(display)
            metadata = {
                **(
                    {
                        "attachments": [
                            {key: item[key] for key in ("name", "url", "publicUrl", "path")}
                            for item in images
                        ],
                        "attachmentContextPath": attachment_path,
                    }
                    if images
                    else {}
                ),
                **({"personalizationCandidate": candidate} if candidate else {}),
            }
            async with database.transaction() as tx:
                message = await insert_message(
                    tx,
                    project_id,
                    "user",
                    display,
                    request_id=identity,
                    conversation=body.get("conversationId"),
                    metadata=metadata,
                )
            selected_model = normalize_model(body.get("selectedModel", project["selected_model"]))
            if body.get("mode") == "chat":
                await answer_only(database, projects, project_id, identity, selected_model, body.get("conversationId"))
                return JSONResponse(
                    {"success": True, "status": "completed", "message": "Answer delivered", "requestId": identity,
                     "userMessageId": message["id"], "conversationId": body.get("conversationId")},
                    status_code=200,
                )
            payload = parse_preparation(
                {
                    "phase": "preparation",
                    "schemaVersion": 1,
                    "finalInstruction": final,
                    "displayInstruction": display,
                    "imageAttachmentInstruction": image_instruction,
                    "selectedModel": selected_model,
                    "cliPreference": "pi",
                    "isInitialPrompt": body["isInitialPrompt"],
                    "conversationId": body.get("conversationId"),
                    "actorUserId": actor,
                    "memorySubjectId": context.actor_user_id,
                    "userMessageId": message["id"],
                    "capabilityId": body.get("capabilityId"),
                    "capabilitySelectionSource": body.get("capabilitySelectionSource"),
                    "processedImages": images,
                }
            )
            envelope = create_finance_envelope(
                payload,
                project_id,
                identity,
                body.get("capabilityId") or FinanceCatalog().data["profile"]["defaultCapabilityId"],
            )
            await Dispatch(database).enqueue(
                project_id,
                identity,
                final,
                execution_envelope=envelope,
                model=selected_model,
                cli="pi",
                max_attempts=1,
                stage="planning_data_prefetch",
            )
        except Exception:
            with suppress(Exception):
                await requests.status(
                    project_id, identity, "failed", "研究请求准备失败，请检查输入后重新提交。"
                )
            raise
        # A projection error cannot turn a durable queue acknowledgement into a failed HTTP request.
        try:
            await Realtime(database).publish(
                project_id, {"type": "message", "data": serialize_message(message)}
            )
        except Exception:
            logging.warning("Queued research message could not be projected for %s", project_id)
        return JSONResponse(
            {
                "success": True,
                "status": "queued",
                "message": "Research request queued",
                "requestId": identity,
                "userMessageId": message["id"],
                "conversationId": body.get("conversationId"),
            },
            status_code=202,
        )

    @router.post("/api/projects/{project_id}/retry-initialization")
    async def retry_initialization(request: Request, project_id: str):
        database, auth = services()
        await auth.require_action(request, "project.update", project_id)
        await auth.require_action(request, "agent.run", project_id)
        projects = Projects(database, root)
        row = await projects.get(project_id)
        try:
            body = await bounded_json(request, 2 * 1048576)
        except ApiError as cause:
            if cause.code != "INVALID_JSON":
                raise
            body = {}
        body = body if isinstance(body, dict) else {}
        instruction = body.get("instruction")
        instruction = (
            instruction.strip()
            if isinstance(instruction, str) and instruction.strip()
            else (row["initial_prompt"] or "").strip()
        )
        if not instruction:
            return JSONResponse(
                {"success": False, "error": "Project has no initial prompt to retry"},
                status_code=400,
            )
        identity = body.get("requestId")
        identity = (
            identity.strip() if isinstance(identity, str) and identity.strip() else uuid4().hex
        )
        selected = normalize_model(body.get("selectedModel", row["selected_model"]))
        updated = await projects.update(
            project_id,
            {
                "status": "initializing",
                "selectedModel": selected,
                "previewUrl": None,
                "previewPort": None,
            },
        )
        await Realtime(database).publish(
            project_id,
            {
                "type": "project_status",
                "data": {
                    "status": "initializing",
                    "message": "正在重新提交初始化任务...",
                    "requestId": identity,
                },
            },
        )
        content = json.dumps(
            {
                "instruction": instruction,
                "displayInstruction": instruction,
                "images": [],
                "isInitialPrompt": True,
                "selectedModel": selected,
                "requestId": identity,
            },
            ensure_ascii=False,
        ).encode()
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": content, "more_body": False}

        try:
            response = await act(Request(dict(request.scope), receive=receive), project_id)
            payload = json.loads(response.body)
        except Exception:
            await projects.update(project_id, {"status": "failed"})
            raise
        if response.status_code >= 400 or not payload.get("success"):
            await projects.update(project_id, {"status": "failed"})
            return JSONResponse(
                {
                    "success": False,
                    "error": payload.get("error", "Retry initialization failed"),
                    "message": payload.get("message", "Failed to restart initial generation"),
                },
                status_code=response.status_code,
            )
        return {
            "success": True,
            "data": {"project": serialize_project(updated), "requestId": identity, "act": payload},
        }

    return router
