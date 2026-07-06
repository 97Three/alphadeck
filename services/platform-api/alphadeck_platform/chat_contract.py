import re
from copy import deepcopy

from .api_errors import ApiError
from .messages import js_length


def parse_act(value):
    fields = {
        "instruction",
        "displayInstruction",
        "conversationId",
        "requestId",
        "selectedModel",
        "images",
        "isInitialPrompt",
        "capabilityId",
        "capabilitySelectionSource",
        "mode",
    }
    if not isinstance(value, dict) or set(value) - fields:
        raise ApiError(400, "INVALID_ACT_REQUEST", "Invalid chat act request.")
    value = deepcopy(value)
    value.setdefault("instruction", "")
    value.setdefault("images", [])
    value.setdefault("isInitialPrompt", False)
    for key in ("instruction", "displayInstruction"):
        if key in value and (not isinstance(value[key], str) or js_length(value[key]) > 200000):
            raise ApiError(400, "INVALID_ACT_REQUEST", key + " must be a bounded string.")
    for key, maximum in (
        ("conversationId", 256),
        ("requestId", 256),
        ("selectedModel", 256),
        ("capabilityId", 128),
    ):
        if key in value:
            if not isinstance(value[key], str) or not 1 <= js_length(value[key].strip()) <= maximum:
                raise ApiError(
                    400, "INVALID_ACT_REQUEST", key + " must be a bounded non-empty string."
                )
            value[key] = value[key].strip()
    if (
        type(value["isInitialPrompt"]) is not bool
        or value.get("mode", "act") not in ("act", "chat")
        or "capabilitySelectionSource" in value
        and value["capabilitySelectionSource"] not in ("manual", "default", "inferred")
    ):
        raise ApiError(400, "INVALID_ACT_REQUEST", "Invalid chat act request.")
    if not isinstance(value["images"], list) or len(value["images"]) > 8:
        raise ApiError(400, "INVALID_ACT_REQUEST", "At most eight image attachments are allowed.")
    for attachment in value["images"]:
        if not isinstance(attachment, dict) or set(attachment) - {"name", "path", "mimeType"}:
            raise ApiError(400, "INVALID_ACT_REQUEST", "Invalid image attachment fields.")
        for key, maximum in (("name", 256), ("path", 2048), ("mimeType", 128)):
            if key in attachment:
                if (
                    not isinstance(attachment[key], str)
                    or not 1 <= js_length(attachment[key].strip()) <= maximum
                ):
                    raise ApiError(
                        400, "INVALID_ACT_REQUEST", "Invalid image attachment " + key + "."
                    )
                attachment[key] = attachment[key].strip()
        path = attachment.get("path", "")
        if (
            not path.startswith("assets/")
            or path[7:] in {"", ".", ".."}
            or any(character in path for character in "\\\0\r\n")
            or "/" in path[7:]
        ):
            raise ApiError(
                400,
                "INVALID_ACT_REQUEST",
                "Attachment path must be a project-relative assets/<filename> path.",
            )
        if "mimeType" in attachment and not re.fullmatch(
            r"image/[a-z0-9.+-]+", attachment["mimeType"], re.I
        ):
            raise ApiError(400, "INVALID_ACT_REQUEST", "Invalid image MIME type.")
    if not (
        value["instruction"].strip()
        or value.get("displayInstruction", "").strip()
        or value["images"]
    ):
        raise ApiError(
            400, "INVALID_ACT_REQUEST", "instruction, displayInstruction or images is required."
        )
    return value


def ingress(instruction, display, request):
    if any(
        len(text.encode("utf-8", "replace")) > 256 * 1024 for text in (instruction, display or "")
    ):
        raise ApiError(413, "INSTRUCTION_TOO_LARGE", "Instruction is too large")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request):
        raise ApiError(400, "INVALID_REQUEST_ID", "Invalid requestId")
