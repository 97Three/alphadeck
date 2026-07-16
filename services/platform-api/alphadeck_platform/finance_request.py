"""Strict durable preparation payload, matching the original ingress contract."""

from copy import deepcopy

from .messages import js_length


def parse_preparation(value):
    text = {
        "finalInstruction": 220000,
        "displayInstruction": 200000,
        "imageAttachmentInstruction": 20000,
        "selectedModel": 512,
        "conversationId": 256,
        "actorUserId": 512,
        "memorySubjectId": 512,
        "userMessageId": 512,
        "capabilityId": 128,
    }
    nullable = {"conversationId", "actorUserId", "capabilityId"}
    fields = {
        *text,
        "phase",
        "schemaVersion",
        "cliPreference",
        "isInitialPrompt",
        "capabilitySelectionSource",
        "processedImages",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Research preparation requires the strict current payload fields.")
    if (
        value["phase"] != "preparation"
        or type(value["schemaVersion"]) is not int
        or value["schemaVersion"] != 1
        or value["cliPreference"] != "pi"
        or type(value["isInitialPrompt"]) is not bool
    ):
        raise ValueError("Unsupported research preparation payload.")
    for key, maximum in text.items():
        item = value[key]
        if key in nullable and item is None:
            continue
        minimum = 0 if key == "imageAttachmentInstruction" else 1
        if not isinstance(item, str) or not minimum <= js_length(item) <= maximum:
            raise ValueError(key + " must be a bounded string.")
    if value["capabilitySelectionSource"] not in (None, "manual", "default", "inferred"):
        raise ValueError("Invalid capability selection source.")
    images = value["processedImages"]
    if not isinstance(images, list) or len(images) > 8:
        raise ValueError("At most eight images are supported.")
    image_fields = {"name": 512, "path": 4096, "url": 4096, "publicUrl": 4096, "mimeType": 128}
    for image in images:
        if not isinstance(image, dict) or set(image) != {*image_fields, "size"}:
            raise ValueError("Image attachment fields are invalid.")
        for key, maximum in image_fields.items():
            if not isinstance(image[key], str) or not 1 <= js_length(image[key]) <= maximum:
                raise ValueError("Invalid image attachment " + key + ".")
        if type(image["size"]) is not int or not 0 <= image["size"] <= 9007199254740991:
            raise ValueError("Image size must be a non-negative safe integer.")
    return deepcopy(value)
