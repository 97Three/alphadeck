import asyncio
import hashlib
import json
import ntpath
import struct
from copy import deepcopy
from pathlib import PurePosixPath

from .config import ROOT
from .file_tools import record as input_record
from .finance_values import coalesce, js_string
from .messages import js_length
from .prefetch_market import timestamp
from .provenance import js_json
from .skill_integrity import read_regular
from .tool_paths import ToolError

CATALOG = json.loads((ROOT / "config/image-tool-catalog.json").read_text())
CONTEXT = ".data-agent/attachments.json"


def parse_image_input(value):
    value = input_record(value)
    for key in value:
        if key not in {"attachmentContextPath", "imagePath", "prompt"}:
            raise ToolError(
                "INVALID_TOOL_INPUT", "Unknown image-extraction input field: " + key + "."
            )
    for key, item in value.items():
        if not isinstance(item, str) or key != "prompt" and not item:
            raise ToolError(
                "INVALID_TOOL_INPUT",
                key + " must be a" + ("" if key == "prompt" else " non-empty") + " string.",
            )
        maximum = 2000 if key == "prompt" else 2048
        if js_length(item) > maximum:
            raise ToolError("INVALID_TOOL_INPUT", f"{key} exceeds {maximum} characters.")
    return value


def image_path(value):
    if js_length(value) > 2048:
        raise ToolError("INVALID_PATH", "Image paths cannot exceed 2048 characters.")
    if any(char in value for char in "\x00\r\n"):
        raise ToolError("INVALID_PATH", "Image paths cannot contain control characters.")
    if value.startswith("/") or ntpath.isabs(value):
        raise ToolError(
            "ABSOLUTE_PATH_DENIED", "PI Agent image tool inputs must use workspace-relative paths."
        )
    return value


def mime_type(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def dimensions(content, mime):
    if mime == "image/png" and len(content) >= 24:
        return struct.unpack(">II", content[16:24])
    if mime == "image/gif" and len(content) >= 10:
        return struct.unpack("<HH", content[6:10])
    if mime == "image/jpeg" and len(content) > 4:
        offset = 2
        while offset + 4 <= len(content):
            if content[offset] != 255:
                offset += 1
                continue
            marker = content[offset + 1]
            if marker in {216, 217}:
                offset += 2
                continue
            size = int.from_bytes(content[offset + 2 : offset + 4], "big")
            if size < 2 or offset + 2 + size > len(content):
                break
            if (
                marker in {192, 193, 194, 195, 197, 198, 199, 201, 202, 203, 205, 206, 207}
                and size >= 7
            ):
                height, width = struct.unpack(">HH", content[offset + 5 : offset + 9])
                return width, height
            offset += 2 + size
    if mime == "image/webp" and len(content) >= 30:
        chunk = content[12:16]
        if chunk == b"VP8X":
            return int.from_bytes(content[24:27], "little") + 1, int.from_bytes(
                content[27:30], "little"
            ) + 1
        if chunk == b"VP8 ":
            width, height = struct.unpack("<HH", content[26:30])
            return width & 16383, height & 16383
        if chunk == b"VP8L" and content[20] == 47:
            bits = int.from_bytes(content[21:25], "little")
            return (bits & 16383) + 1, ((bits >> 14) & 16383) + 1
    return None, None


class ImageTool:
    def __init__(
        self,
        policy,
        *,
        timeout_ms=15000,
        max_image_bytes=26214400,
        max_attachments=12,
        max_output_chars=12000,
    ):
        self.policy, self.timeout, self.maximum, self.count, self.output = (
            policy,
            timeout_ms,
            max_image_bytes,
            max_attachments,
            max_output_chars,
        )
        self.definition = deepcopy(CATALOG["definition"])

    def attachment_context(self, relative):
        resolved = self.policy.read(image_path(relative))
        if not resolved.canonical.is_file():
            raise ToolError("NOT_A_FILE", f"Attachment context is not a file: {relative}.")
        if resolved.canonical.stat().st_size > 1048576:
            raise ToolError(
                "ATTACHMENT_CONTEXT_TOO_LARGE", "Attachment context exceeds 1048576 bytes."
            )
        try:
            value = json.loads(read_regular(resolved.canonical, 1048576))
        except ValueError as cause:
            raise ToolError(
                "INVALID_ATTACHMENT_CONTEXT", f"Attachment context is not valid JSON: {relative}."
            ) from cause
        if not isinstance(value, dict):
            raise ToolError(
                "INVALID_ATTACHMENT_CONTEXT", "Attachment context must be a JSON object."
            )
        attachments = value.get("attachments", [])
        if not isinstance(attachments, list):
            raise ToolError("INVALID_ATTACHMENT_CONTEXT", "attachments must be an array.")
        if any(not isinstance(item, dict) for item in attachments):
            raise ToolError(
                "INVALID_ATTACHMENT_CONTEXT", "Every attachments entry must be a JSON object."
            )
        return resolved.canonical_relative, attachments

    def inspect(self, attachment, index):
        source = attachment.get("path")
        if not isinstance(source, str) or not source:
            raise ToolError(
                "INVALID_ATTACHMENT",
                "Attachment "
                + js_string(coalesce(attachment.get("name"), index + 1))
                + " is missing workspace-relative path.",
            )
        resolved = self.policy.read(image_path(source))
        path = resolved.canonical
        if not path.is_file():
            raise ToolError("NOT_A_FILE", f"Image attachment is not a file: {source}.")
        size = path.stat().st_size
        if size > self.maximum:
            raise ToolError(
                "IMAGE_TOO_LARGE",
                "Image "
                + js_string(coalesce(attachment.get("name"), resolved.relative))
                + f" exceeds {self.maximum} bytes.",
                {"size": size, "maxImageBytes": self.maximum},
            )
        content = read_regular(path, self.maximum)
        mime = mime_type(content)
        if mime is None:
            raise ToolError(
                "UNSUPPORTED_IMAGE",
                "Attachment is not a supported PNG, JPEG, GIF, or WebP image: "
                + resolved.relative
                + ".",
            )
        width, height = dimensions(content, mime)

        def bounded(key, maximum):
            value = attachment.get(key)
            if value is None:
                return None
            if not isinstance(value, str) or js_length(value) > maximum:
                raise ToolError(
                    "INVALID_ATTACHMENT",
                    f"attachment.{key} must be a string of at most {maximum} characters.",
                )
            return value

        return {
            "id": bounded("id", 128) or "image-" + str(index + 1),
            "name": bounded("name", 256) or path.name,
            "path": resolved.canonical_relative,
            "url": bounded("url", 2048),
            "publicUrl": bounded("publicUrl", 2048),
            "mimeType": mime,
            "size": size,
            "width": width,
            "height": height,
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def extract(self, value):
        if type(self.maximum) is not int or self.maximum <= 0:
            raise ToolError("INVALID_LIMIT", "maxImageBytes must be a positive integer.")
        if type(self.count) is not int or not 1 <= self.count <= 100:
            raise ToolError("INVALID_LIMIT", "maxAttachments must be an integer between 1 and 100.")
        if type(self.output) is not int or self.output < 1024:
            raise ToolError("INVALID_LIMIT", "maxOutputChars must be an integer of at least 1024.")
        context = value.get("attachmentContextPath", CONTEXT)
        if value.get("imagePath"):
            path = image_path(value["imagePath"])
            attachments = [
                {"id": "image-1", "name": PurePosixPath(value["imagePath"]).name, "path": path}
            ]
            context = image_path(context)
        else:
            context, attachments = self.attachment_context(context)
        if len(attachments) > self.count:
            raise ToolError(
                "TOO_MANY_ATTACHMENTS",
                f"Image extraction accepts at most {self.count} attachments.",
                {"count": len(attachments), "maxAttachments": self.count},
            )
        if not attachments:
            return {
                "schemaVersion": 1,
                "runtime": "PI Agent",
                "tool": "image-extraction",
                "status": "no_attachments",
                "message": f"未找到上传图片附件，请确认 {context} 是否包含 attachments。",
            }
        payload = deepcopy(CATALOG["payload"])
        payload.update(
            createdAt=timestamp(),
            attachmentContextPath=context,
            prompt=value.get("prompt"),
            images=[
                self.inspect(attachment, index) for index, attachment in enumerate(attachments)
            ],
        )
        length = js_length(js_json(payload))
        if length > self.output:
            raise ToolError(
                "TOOL_OUTPUT_TOO_LARGE",
                f"Image metadata output exceeds the PI Agent {self.output}-character limit.",
                {
                    "payloadCharacters": length,
                    "maxOutputChars": self.output,
                    "imageCount": len(payload["images"]),
                },
            )
        return payload

    async def execute(self, value, context=None):
        try:
            async with asyncio.timeout(self.timeout / 1000):
                data = self.extract(parse_image_input(value))
                count = len(data["images"]) if data["status"] == "metadata_ready" else 0
                return {
                    "ok": True,
                    "data": data,
                    "content": f"PI Agent verified {count} uploaded image(s). OCR is disabled; uncertain portfolio fields require manual confirmation."
                    if data["status"] == "metadata_ready"
                    else data["message"],
                    "metadata": {
                        "runtime": "PI Agent",
                        "status": data["status"],
                        "imageCount": count,
                    },
                }
        except TimeoutError:
            error = ToolError("TOOL_TIMEOUT", f"PI Agent tool timed out after {self.timeout}ms.")
        except ToolError as cause:
            error = cause
        except Exception as cause:
            error = ToolError("TOOL_EXECUTION_FAILED", str(cause))
        return {
            "ok": False,
            "error": {
                "code": error.code,
                "message": error.message,
                **({"details": error.details} if error.details is not None else {}),
            },
            "content": error.message,
        }
