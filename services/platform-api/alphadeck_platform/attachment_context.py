from .image_assets import ImageAssetError, maximum_bytes, normalize_attachment
from .prefetch_market import timestamp
from .provenance import js_json
from .resource_lock import resource_lock, sync_directory
from .scaffold import controlled_path
from .workspace import write_atomic

RELATIVE = ".data-agent/attachments.json"


async def prepare_attachments(root, project, request, images):
    processed, total = [], 0
    for index, attachment in enumerate(images):
        item = await normalize_attachment(root, project, attachment, index)
        total += item["size"]
        if total > min(25 * 1048576, 8 * maximum_bytes()):
            raise ImageAssetError("Image attachments exceed the 25MB total limit", 413)
        processed.append(item)
    if not processed:
        return [], None, ""
    manifest = {
        "extractionContract": {
            "requiredSkill": "image-extraction",
            "requiredTool": "quant_extract_uploaded_image",
            "portfolioScreenshotFields": [
                "account_total_asset",
                "cash_available",
                "market_value",
                "daily_pnl",
                "total_pnl",
                "position_ratio",
                "holdings[].name",
                "holdings[].symbol_if_visible_or_resolved",
                "holdings[].quantity",
                "holdings[].cost_price",
                "holdings[].current_price",
                "holdings[].market_value",
                "holdings[].pnl",
                "holdings[].pnl_percent",
            ],
            "rule": "无法确定的截图字段必须写 null，并在 evidence/data_quality.json 说明不确定性，不允许编造。",
        },
        "schemaVersion": 1,
        "projectId": project,
        "requestId": request,
        "createdAt": timestamp(),
        "instruction": "这些图片由用户随本次问题上传。Agent 必须先读取本文件并检查图片，再解析其中的股票、持仓、成本、现金、盈亏、仓位等字段。",
        "attachments": [
            {"id": f"image-{index + 1}", **item} for index, item in enumerate(processed)
        ],
    }
    async with resource_lock(root, {"purpose": "image_manifest"}, recover_dead_local_owner=True):
        controlled_path(root, RELATIVE)
        target = write_atomic(root, RELATIVE, (js_json(manifest, indent=2) + "\n").encode())
        sync_directory(target.parent)
    listed = "\n".join(
        f"{index + 1}. {item['name']}：{item['path']}" for index, item in enumerate(processed)
    )
    instruction = f"""
用户为本次任务上传了 {len(processed)} 张图片。
- 附件清单：{RELATIVE}
- 必须先调用 quant_extract_uploaded_image 读取每张图片，再结合量化数据接口生成结果。
- 当前不接入额外视觉模型或第三方 OCR；无法可靠识别的截图字段必须写 null，并在证据文件中列出需要用户确认的内容。
- 对识别出的股票名称必须使用 quant-symbol-resolver 或 /api/v1/symbols/resolve 解析代码，再获取真实行情、K 线、指标和必要的基本面数据。
- 必须把图片提取结果写入 evidence/image_extraction.json；没有 OCR/视觉结果时也要写明 visualRecognition.status 和 needs_manual_confirmation。
- 最终 dashboard-data.json 必须保留 portfolio、holdings、assets、comparison 和 imageExtraction 字段；imageExtraction 要说明哪些字段来自截图识别、哪些来自行情接口补全。
- 如果当前运行时无法直接识别图片视觉内容，也必须基于附件清单和文件路径继续处理，并明确列出需要人工确认的截图字段。

图片路径：
{listed}"""
    return processed, RELATIVE, instruction
