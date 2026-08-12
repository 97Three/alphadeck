import base64
import json
from pathlib import Path

import pytest

from alphadeck_platform.api_errors import ApiError
from alphadeck_platform.chat_contract import parse_act
from alphadeck_platform.finance_request import parse_preparation
from alphadeck_platform.image_assets import ImageAssetError, decode_base64
from alphadeck_platform.workspace_progress import build_message

CASES = json.loads((Path(__file__).parent / "fixtures/chat-progress.json").read_text())


@pytest.mark.parametrize("case", CASES["messages"])
def test_page_progress_messages_match_original(case):
    assert build_message(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["preparations"])
def test_durable_preparation_validation_matches_original(case):
    if not case["ok"]:
        with pytest.raises((ValueError, TypeError)):
            parse_preparation(case["input"])
    else:
        assert parse_preparation(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["acts"])
def test_browser_ingress_contract_matches_original(case):
    if not case["ok"]:
        with pytest.raises(ApiError):
            parse_act(case["input"])
    else:
        assert parse_act(case["input"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["images"])
def test_image_signature_and_base64_contract_matches_original(case):
    if "error" in case:
        with pytest.raises(ImageAssetError) as caught:
            decode_base64(case["input"])
        assert caught.value.message == case["error"]
        assert caught.value.status == case["status"]
    else:
        assert base64.b64encode(decode_base64(case["input"])).decode() == case["expected"]
