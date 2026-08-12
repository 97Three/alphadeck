import asyncio

import pytest

from alphadeck_platform.answer_only import complete_text
from alphadeck_platform.chat_contract import parse_act


class TextProvider:
    async def complete(self, request):
        assert request["messages"][0]["role"] == "system"
        assert request["messages"][-1] == {"role": "user", "content": "什么是收盘价？"}
        yield {"type": "text_delta", "delta": "收盘价是"}
        yield {"type": "text_delta", "delta": "交易日结束时的价格。"}


def test_answer_only_mode_is_explicit_and_bounded():
    parsed = parse_act({"instruction": "什么是收盘价？", "mode": "chat"})
    assert parsed["mode"] == "chat"
    with pytest.raises(Exception):  # noqa: B017
        parse_act({"instruction": "问题", "mode": "other"})
    answer = asyncio.run(complete_text(TextProvider(), "deepseek-v4-flash", [{"role": "user", "content": "什么是收盘价？"}]))
    assert answer == "收盘价是交易日结束时的价格。"
