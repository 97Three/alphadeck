import base64
import math
import os
import re
from uuid import uuid4

from .api_errors import ApiError
from .resource_lock import resource_lock, sync_directory
from .scaffold import controlled_path
from .skill_integrity import read_regular
from .workspace import write_atomic

MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


class ImageAssetError(ApiError):
    def __init__(self, message, status=400):
        super().__init__(status, "INVALID_IMAGE_ASSET", message)


def maximum_bytes(value=None):
    try:
        parsed = float(
            os.getenv("ALPHADECK_MAX_IMAGE_UPLOAD_BYTES", "") if value is None else value
        )
        return (
            min(math.floor(parsed), 50 * 1024 * 1024)
            if math.isfinite(parsed) and parsed > 0
            else 10 * 1024 * 1024
        )
    except (ValueError, TypeError):
        return 10 * 1024 * 1024


def image_type(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def validate(content, declared=None, maximum=None):
    maximum = maximum_bytes() if maximum is None else maximum
    if not content:
        raise ImageAssetError("Image cannot be empty")
    if len(content) > maximum:
        raise ImageAssetError(f"Image must be smaller than {maximum // 1048576}MB", 413)
    detected = image_type(content)
    if detected is None:
        raise ImageAssetError("File content must be a PNG, JPEG, WebP, or GIF image")
    if declared and declared != detected:
        raise ImageAssetError("File content does not match the declared image type")
    return detected


def decode_base64(value, *, required=None, maximum=None):
    maximum = maximum_bytes() if maximum is None else maximum
    match = re.fullmatch(r"data:([^;,]+);base64,(.*)", value.strip(), re.S)
    declared, raw = (match[1], match[2]) if match else (None, value)
    raw = re.sub(r"\s", "", raw)
    if not raw or not re.fullmatch(r"[a-zA-Z0-9+/]*={0,2}", raw) or len(raw) % 4 == 1:
        raise ImageAssetError("Image payload must be valid base64")
    if len(raw) * 3 // 4 > maximum:
        raise ImageAssetError(f"Image must be smaller than {maximum // 1048576}MB", 413)
    try:
        content = base64.b64decode(raw.ljust((len(raw) + 3) // 4 * 4, "="), validate=True)
    except ValueError as cause:
        raise ImageAssetError("Image payload must be valid base64") from cause
    if base64.b64encode(content).decode().rstrip("=") != raw.rstrip("="):
        raise ImageAssetError("Image payload must be valid base64")
    detected = validate(content, declared, maximum)
    if required and detected != required:
        raise ImageAssetError("Image content must be " + required)
    return content


def asset_path(root, filename):
    if not isinstance(filename, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", filename
    ):
        raise ImageAssetError("Invalid asset filename")
    try:
        return controlled_path(root, "assets/" + filename)
    except ValueError as cause:
        raise ImageAssetError("Invalid asset path") from cause


def read_asset(root, filename):
    try:
        content = read_regular(asset_path(root, filename), maximum_bytes())
    except FileNotFoundError as cause:
        raise ImageAssetError("Image asset not found", 404) from cause
    except ValueError as cause:
        raise ImageAssetError("Image asset is unsafe or too large", 413) from cause
    return content, validate(content)


async def save_asset(root, content, declared, *, logo=False):
    detected = validate(content, declared)
    filename = "logo.png" if logo else str(uuid4()) + MIME[detected]
    if logo and detected != "image/png":
        raise ImageAssetError("Image content must be image/png")
    async with resource_lock(root, {"purpose": "image_upload"}, recover_dead_local_owner=True):
        asset_path(root, filename)
        path = write_atomic(root, "assets/" + filename, content)
        sync_directory(path.parent)
        if not logo:
            controlled_path(root, "public/uploads/" + filename)
            mirror = write_atomic(root, "public/uploads/" + filename, content)
            sync_directory(mirror.parent)
    return filename


async def normalize_attachment(root, project, attachment, index):
    relative = attachment["path"]
    filename = relative.removeprefix("assets/")
    if relative != "assets/" + filename or "/" in filename or "\\" in filename:
        raise ImageAssetError(
            "Asset path must identify a file directly inside this project assets folder"
        )
    content, detected = read_asset(root, filename)
    validate(content, attachment.get("mimeType"))
    async with resource_lock(root, {"purpose": "image_mirror"}, recover_dead_local_owner=True):
        controlled_path(root, "public/uploads/" + filename)
        mirror = write_atomic(root, "public/uploads/" + filename, content)
        sync_directory(mirror.parent)
    return {
        "name": attachment.get("name") or f"Image {index + 1}",
        "path": relative,
        "url": f"/api/assets/{project}/{filename}",
        "publicUrl": "/uploads/" + filename,
        "mimeType": detected,
        "size": len(content),
    }
