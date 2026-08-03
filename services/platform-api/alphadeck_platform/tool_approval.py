import hashlib
import math
import re
import secrets
from copy import deepcopy

from .context_capsule import safe_integer, utf8
from .messages import js_length
from .provenance import canonical_json
from .tool_paths import ToolError

FORBIDDEN = {
    "authorization",
    "cookie",
    "setcookie",
    "apikey",
    "secret",
    "password",
    "accesstoken",
    "refreshtoken",
    "privatekey",
    "credential",
    "credentials",
    "reasoning",
    "reasoningcontent",
    "chainofthought",
    "systemprompt",
    "messages",
    "rawrequest",
    "rawresponse",
}
DECISIONS = ("approve", "edit", "reject")


def error(code, message):
    raise ToolError(code, message)


def public_input(value):
    nodes, ancestors = 0, set()

    def visit(candidate, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 5000 or depth > 16:
            error(
                "INVALID_TOOL_APPROVAL_INPUT", "Approval input exceeds its JSON structural budget."
            )
        if candidate is None or type(candidate) in {str, bool}:
            return
        if type(candidate) in {float, int}:
            if not math.isfinite(candidate):
                error("INVALID_TOOL_APPROVAL_INPUT", "Approval input contains a non-finite number.")
            return
        if type(candidate) not in {dict, list} or id(candidate) in ancestors:
            error("INVALID_TOOL_APPROVAL_INPUT", "Approval input must contain plain acyclic JSON.")
        ancestors.add(id(candidate))
        try:
            if isinstance(candidate, dict):
                for key, child in candidate.items():
                    if (
                        not isinstance(key, str)
                        or not 0 < js_length(key) <= 160
                        or key in {"__proto__", "prototype", "constructor"}
                        or re.sub(r"[_\-.\s]", "", key).lower() in FORBIDDEN
                    ):
                        error(
                            "INVALID_TOOL_APPROVAL_INPUT",
                            "Approval input contains a forbidden key.",
                        )
                    visit(child, depth + 1)
            else:
                for child in candidate:
                    visit(child, depth + 1)
        finally:
            ancestors.remove(id(candidate))

    visit(value)
    if type(value) is not dict or len(canonical_json(value)) > 65536:
        error(
            "INVALID_TOOL_APPROVAL_INPUT",
            "Approval input must be a JSON object within 65536 bytes.",
        )
    return deepcopy(value)


def decisions(policy):
    values = policy.get("allowedDecisions", ["approve", "reject"])
    if (
        not isinstance(values, (list, tuple))
        or any(value not in DECISIONS for value in values)
        or "approve" not in values
        or "reject" not in values
    ):
        error(
            "INVALID_TOOL_APPROVAL_POLICY",
            "Tool approval decisions must contain approve and reject, with edit optional.",
        )
    return [decision for decision in DECISIONS if decision in values]


def validate_policy(definition):
    policy = definition.get("approval")
    if policy is None:
        return
    if definition["effect"] not in {"workspace_write", "external_write"} or not isinstance(
        policy, dict
    ):
        error("INVALID_TOOL_APPROVAL_POLICY", "Only mutating tools can require approval.")
    reason = policy.get("reason")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or js_length(reason.strip()) > 500
        or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", reason)
    ):
        error("INVALID_TOOL_APPROVAL_POLICY", "Tool approval reason is invalid.")
    if not callable(policy.get("projectPublicInput")):
        error(
            "INVALID_TOOL_APPROVAL_POLICY",
            "Approval requires an application-owned public input projector.",
        )
    timeout = policy.get("timeoutMs", 600000)
    if not safe_integer(timeout, 1) or timeout > 86400000:
        error(
            "INVALID_TOOL_APPROVAL_POLICY", "Tool approval timeout is outside the supported range."
        )
    decisions(policy)


def create_request(run_id, turn, call, definition, arguments, now):
    policy = definition["approval"]
    source = public_input(arguments)
    projected = public_input(policy["projectPublicInput"](source))
    return {
        "approvalId": "approval_" + secrets.token_urlsafe(18),
        "runId": run_id,
        "turn": turn,
        "toolCallId": call["id"],
        "toolName": call["name"],
        "effect": definition["effect"],
        "idempotency": definition["idempotency"],
        "inputSha256": hashlib.sha256(utf8(call["arguments"])).hexdigest(),
        "publicInput": projected,
        "reason": policy["reason"].strip(),
        "allowedDecisions": decisions(policy),
        "requestedAt": now,
        "expiresAt": now + policy.get("timeoutMs", 600000),
    }


def apply_resolution(request, resolution, definition, call):
    if (
        not isinstance(resolution, dict)
        or resolution.get("decision") not in request["allowedDecisions"]
    ):
        error("INVALID_TOOL_APPROVAL_RESOLUTION", "Approval decision is not allowed.")
    actor = resolution.get("resolvedBy")
    if actor is not None and (
        not isinstance(actor, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+/@=-]{0,255}", actor)
    ):
        error(
            "INVALID_TOOL_APPROVAL_RESOLUTION",
            "Approval resolver must be a bounded public identifier.",
        )
    effective = dict(call)
    if resolution["decision"] == "edit":
        edited = public_input(resolution.get("editedInput"))
        projected = public_input(definition["approval"]["projectPublicInput"](edited))
        if canonical_json(projected) != canonical_json(edited):
            error(
                "INVALID_TOOL_APPROVAL_RESOLUTION",
                "Editable approval input must be the complete public tool input.",
            )
        effective["arguments"] = canonical_json(edited).decode()
    elif "editedInput" in resolution:
        error(
            "INVALID_TOOL_APPROVAL_RESOLUTION", "editedInput is allowed only for an edit decision."
        )
    return effective, hashlib.sha256(utf8(effective["arguments"])).hexdigest()


class DurableApprovalHandler:
    def __init__(self, database, project):
        from .approvals import Approvals

        self.approvals, self.project = Approvals(database), project

    async def __call__(self, request, *, cancel=None):
        await self.approvals.create(request)
        return await self.approvals.wait(self.project, request["approvalId"], cancel=cancel)
