import hashlib
import struct
from pathlib import PurePosixPath

from .baseline_evidence import QUALITY, SOURCES
from .finance_values import coalesce, js_string, record
from .prefetch_market import timestamp
from .scaffold import controlled_path
from .skill_integrity import read_regular

FIELDS = [
    "account_total_asset",
    "cash_available",
    "market_value",
    "daily_pnl",
    "total_pnl",
    "position_ratio",
    "holdings[].name",
    "holdings[].quantity",
    "holdings[].cost_price",
    "holdings[].current_price",
    "holdings[].market_value",
    "holdings[].pnl",
    "holdings[].pnl_percent",
]


def image_mime(path, content):
    path = path.lower()
    if content.startswith(b"\x89PNG\r\n\x1a\n") or path.endswith(".png"):
        return "image/png"
    if content.startswith(b"\xff\xd8") or path.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if content.startswith(b"GIF") or path.endswith(".gif"):
        return "image/gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def image_size(content, mime):
    if mime == "image/png" and len(content) >= 24:
        return struct.unpack(">II", content[16:24])
    if mime == "image/gif" and len(content) >= 10:
        return struct.unpack("<HH", content[6:10])
    if mime == "image/jpeg" and len(content) > 4:
        offset = 2
        while offset + 9 < len(content):
            if content[offset] != 255:
                offset += 1
                continue
            marker = content[offset + 1]
            length = int.from_bytes(content[offset + 2 : offset + 4], "big")
            if marker in {192, 193, 194, 195, 197, 198, 199, 201, 202, 203, 205, 206, 207}:
                height, width = struct.unpack(">HH", content[offset + 5 : offset + 9])
                return width, height
            offset += max(2 + length, 2)
    return None, None


async def image_extraction(artifacts, run, warnings, *, now=None):
    context = artifacts.read(".data-agent/attachments.json") or {}
    attachments = [item for item in context.get("attachments", []) if isinstance(item, dict)]
    images = []
    if len(attachments) > 32:
        raise ValueError("Too many uploaded image attachments")
    for index, attachment in enumerate(attachments):
        source = attachment.get("path") if isinstance(attachment.get("path"), str) else None
        if not source:
            warnings.append(
                "上传图片附件 "
                + js_string(coalesce(attachment.get("name"), index + 1))
                + " 缺少工作空间相对 path。"
            )
            continue
        try:
            path = controlled_path(artifacts.root, source)
            content = read_regular(path, 33554432)
            mime = attachment.get("mimeType")
            mime = (
                mime.strip()
                if isinstance(mime, str) and mime.strip()
                else image_mime(source, content)
            )
            width, height = image_size(content, mime)
            images.append(
                {
                    "id": js_string(coalesce(attachment.get("id"), "image-" + str(index + 1))),
                    "name": js_string(coalesce(attachment.get("name"), PurePosixPath(source).name)),
                    "path": path.relative_to(artifacts.root).as_posix(),
                    "url": attachment.get("url")
                    if isinstance(attachment.get("url"), str)
                    else None,
                    "publicUrl": attachment.get("publicUrl")
                    if isinstance(attachment.get("publicUrl"), str)
                    else None,
                    "mimeType": mime,
                    "size": len(content),
                    "width": width,
                    "height": height,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        except (OSError, ValueError) as cause:
            warnings.append(
                "上传图片附件 "
                + js_string(coalesce(attachment.get("name"), source))
                + " 检查失败："
                + str(cause)
            )
    if not images:
        return None
    now = now or timestamp()
    payload = {
        "schemaVersion": 1,
        "tool": "image-extraction",
        "status": "metadata_ready",
        "createdAt": now,
        "runId": run,
        "attachmentContextPath": ".data-agent/attachments.json",
        "images": images,
        "visualRecognition": {
            "status": "requires_vision_provider",
            "reason": "平台已确认图片文件、格式、尺寸和哈希；若当前模型或工具不能直接 OCR，必须保留截图字段缺口并要求用户确认。",
            "fallbackRule": "不得编造持仓数量、成本、现金、盈亏和仓位比例；未识别字段必须写入 data_gaps 和 evidence/data_quality.json。",
        },
        "imageExtraction": {
            "source": "uploaded_image",
            "extracted_at": now,
            "extractedFields": {**{key: None for key in FIELDS[:6]}, "holdings": []},
            "needs_manual_confirmation": True,
            "manual_confirmation_fields": FIELDS,
        },
        "dashboardContract": {
            "requiredFinalDataFields": [
                "portfolio",
                "holdings",
                "assets",
                "comparison",
                "imageExtraction",
            ],
            "evidenceFiles": ["evidence/image_extraction.json", QUALITY, SOURCES],
        },
    }
    await artifacts.write("evidence/image_extraction.json", payload)
    await artifacts.event(
        {
            "event_type": "image_attachment_evidence_created",
            "stage": "data_collection",
            "status": "warning",
            "run_id": run,
            "artifact_path": "evidence/image_extraction.json",
            "summary": "已为上传图片生成附件证据和截图识别契约；未确认字段需要在看板中标注。",
            "created_at": now,
        }
    )
    warnings.append(
        "上传图片已建立附件证据；截图 OCR 字段未完全确认，页面必须展示数据缺口和人工确认要求。"
    )
    return payload


async def augment_image_evidence(artifacts, evidence):
    created = (
        evidence.get("createdAt") if isinstance(evidence.get("createdAt"), str) else timestamp()
    )
    images = evidence.get("images") if isinstance(evidence.get("images"), list) else []
    quality, sources = artifacts.read(QUALITY) or {}, artifacts.read(SOURCES) or {}
    item = {
        "id": "uploaded_image_attachment",
        "name": "用户上传持仓截图",
        "source": "uploaded_image",
        "endpoint": "UPLOAD .data-agent/attachments.json",
        "artifact_path": "evidence/image_extraction.json",
        "row_count": len(images),
        "fetched_at": created,
        "as_of": created,
        "missing_fields": FIELDS,
        "warnings": ["截图文件已确认，但 OCR 字段需要模型或用户继续确认，不允许编造持仓明细。"],
        "status": "warning",
        "required": True,
    }

    def append_unique(parent, key, value):
        rows = parent.get(key) if isinstance(parent.get(key), list) else []
        if not any((record(row) or {}).get("id") == value["id"] for row in rows):
            rows.append(value)
        parent[key] = rows

    quality["status"] = "error" if quality.get("status") == "error" else "warning"
    append_unique(quality, "datasets", item)
    append_unique(
        quality,
        "checks",
        {
            "id": "uploaded_image_attachment_quality",
            "dataset": item["id"],
            "status": "warning",
            "row_count": len(images),
            "missing_fields": FIELDS,
            "summary": "用户上传截图已进入证据链，关键持仓字段仍需 OCR 或用户确认。",
        },
    )
    for key, message in (
        ("warnings", "用户上传截图字段未完全结构化，调仓分析需显式标注截图识别边界。"),
        (
            "limitations",
            "截图类任务必须区分图片元数据、行情接口数据和模型推断，不能把未确认字段当作真实持仓。",
        ),
    ):
        quality[key] = [*(quality[key] if isinstance(quality.get(key), list) else []), message]
    append_unique(
        sources,
        "sources",
        {
            "id": item["id"],
            "dataset": item["name"],
            **{
                key: item[key]
                for key in (
                    "source",
                    "endpoint",
                    "artifact_path",
                    "row_count",
                    "fetched_at",
                    "as_of",
                    "status",
                )
            },
        },
    )
    await artifacts.write(QUALITY, quality)
    await artifacts.write(SOURCES, sources)
