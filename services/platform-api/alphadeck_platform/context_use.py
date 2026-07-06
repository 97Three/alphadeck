import json
import re
from copy import deepcopy

from .integration_config import integration_scope
from .messages import js_length
from .prefetch_market import timestamp
from .provenance import js_json
from .resource_lock import sync_directory
from .scaffold import controlled_path
from .skill_integrity import read_regular
from .workspace import write_atomic


def stable_unique(values):
    return sorted(set(values), key=lambda value: value.encode("utf-16-be", "surrogatepass"))


def safe_request(value):
    value = value.strip()
    if not value or js_length(value) > 160 or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        raise ValueError("Context use manifest request ID is invalid.")
    return value


def scope_use(scope, project):
    if scope["projectId"] != project:
        raise ValueError("Context use integration scope does not match the workspace project.")
    return {
        "consumerId": scope["consumerId"],
        "memoryTenantId": scope["memory"]["tenantId"],
        "projectId": project,
        "scopeSha256": scope["scopeSha256"],
        "modelPort": scope["modelPort"],
        "requestedKnowledgeSpaceIds": stable_unique(scope["knowledge"]["requestedSpaceIds"]),
        "projectKnowledgeSpaceId": scope["knowledge"]["projectSpaceId"],
    }


def memory_use(capsule):
    if capsule is None:
        return None
    if not capsule.get("usageId"):
        raise ValueError("Exposed personal memory is missing its provider usage receipt.")
    return {
        "provider": "evolvable-memory-http-v1",
        **{key: capsule[key] for key in ("usageId", "traceId")},
        "revisionIds": stable_unique(capsule["revisionIds"]),
        "sourceProjectionSha256": capsule["sourceProjectionSha256"],
        "deliveredContextSha256": capsule["contentSha256"],
    }


def knowledge_use(capsule):
    if capsule is None:
        return None
    return {
        "provider": "akep-http-v0.1",
        **{
            key: capsule[key]
            for key in ("contextPackId", "exposureReceiptId", "contextDigest", "policyEpoch")
        },
        **{
            key: stable_unique([item[source] for item in capsule["citations"]])
            for key, source in (
                ("citationIds", "citationId"),
                ("revisionIds", "revisionId"),
                ("spaceIds", "spaceId"),
            )
        },
        "usage": None,
    }


def read_manifest(root, project, request, scope):
    relative = "evidence/context-uses/" + safe_request(request) + ".json"
    try:
        value = json.loads(read_regular(controlled_path(root, relative), 8388608))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("Context use manifest scope or schema does not match.")
    if (
        value.get("schemaVersion") == 1
        and value.get("projectId") == project
        and value.get("requestId") == request
    ):
        return {**value, "schemaVersion": 2, "integrationScope": scope}
    if (
        value.get("schemaVersion") != 2
        or value.get("projectId") != project
        or value.get("requestId") != request
        or js_json(value.get("integrationScope")) != js_json(scope)
    ):
        raise ValueError("Context use manifest scope or schema does not match.")
    return value


def write_manifest(root, value):
    relative = "evidence/context-uses/" + safe_request(value["requestId"]) + ".json"
    controlled_path(root, relative)
    target = write_atomic(root, relative, (js_json(value, indent=2) + "\n").encode())
    sync_directory(target.parent)


async def record_exposure(artifacts, project, request, scope, memory=None, knowledge=None):
    request = safe_request(request)
    selected_memory, selected_knowledge, selected_scope = (
        memory_use(memory),
        knowledge_use(knowledge),
        scope_use(scope, project),
    )
    if knowledge and (
        knowledge["integrationScopeSha256"] != selected_scope["scopeSha256"]
        or stable_unique(knowledge["requestedSpaceIds"])
        != selected_scope["requestedKnowledgeSpaceIds"]
    ):
        raise ValueError("Knowledge capsule does not match the workspace integration scope.")

    async def operation(_tx):
        current = read_manifest(artifacts.root, project, request, selected_scope)
        if selected_memory is None and selected_knowledge is None and current is None:
            return False
        current_knowledge = (
            {**current["knowledge"], "usage": None}
            if current and current.get("knowledge")
            else None
        )
        if current and (
            js_json(current["integrationScope"]) != js_json(selected_scope)
            or js_json(current.get("memory")) != js_json(selected_memory)
            or js_json(current_knowledge) != js_json(selected_knowledge)
        ):
            raise ValueError("Context exposure idempotency collision.")
        now = timestamp()
        write_manifest(
            artifacts.root,
            current
            or {
                "schemaVersion": 2,
                "projectId": project,
                "requestId": request,
                "integrationScope": selected_scope,
                "memory": selected_memory,
                "knowledge": selected_knowledge,
                "acceptedMission": None,
                "createdAt": now,
                "updatedAt": now,
            },
        )
        return True

    return await artifacts.mutate(operation)


async def record_acceptance(
    artifacts, project, request, mission, knowledge_usage=None, *, scope=None
):
    request = safe_request(request)
    selected_scope = scope_use(scope if scope is not None else integration_scope(project), project)

    async def operation(_tx):
        current = read_manifest(artifacts.root, project, request, selected_scope)
        if current is None:
            return False
        if current.get("acceptedMission") and js_json(current["acceptedMission"]) != js_json(
            mission
        ):
            raise ValueError("Context acceptance idempotency collision.")
        knowledge = deepcopy(current.get("knowledge"))
        if (
            knowledge
            and knowledge.get("usage")
            and knowledge_usage is not None
            and js_json(knowledge["usage"]) != js_json(knowledge_usage)
        ):
            raise ValueError("Knowledge outcome evidence idempotency collision.")
        if knowledge:
            knowledge["usage"] = knowledge.get("usage") or knowledge_usage
        write_manifest(
            artifacts.root,
            {
                **current,
                "knowledge": knowledge,
                "acceptedMission": mission,
                "updatedAt": timestamp(),
            },
        )
        return True

    return await artifacts.mutate(operation)
