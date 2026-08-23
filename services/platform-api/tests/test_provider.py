import asyncio
import json
from pathlib import Path

import httpx
import pytest

from alphadeck_platform.provider import ChatProvider, ProviderError

CASES = json.loads((Path(__file__).parent / "fixtures/provider.json").read_text())


class Fragmented(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        for index in range(0, len(self.content), 7):
            yield self.content[index : index + 7]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["scenario"]["name"])
def test_model_request_and_stream_events_match_original_typescript(case):
    scenario = case["scenario"]
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if scenario.get("retry") and len(requests) == 1:
            return httpx.Response(503, content=b"retry", headers={"retry-after": "0"})
        return httpx.Response(
            200,
            stream=Fragmented(case["stream"].encode()),
            headers={"content-type": "text/event-stream"},
        )

    async def no_wait(_delay):
        pass

    async def run():
        events = []
        error = None
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ChatProvider(
                "fixture-key",
                client=client,
                deepseek=not scenario.get("generic", False),
                limits=scenario.get("limits"),
                max_retries=1 if scenario.get("retry") else 0,
                sleep=no_wait,
                random_source=lambda: 0,
            )
            try:
                async for event in provider.complete(case["request"]):
                    events.append(event)
            except ProviderError as cause:
                error = {
                    "code": cause.code,
                    **({"status": cause.status} if cause.status is not None else {}),
                }
        assert events == case["expected"]["events"]
        assert error == case["expected"]["error"]
        assert requests == case["expected"]["requests"]

    asyncio.run(run())


def test_transport_failure_after_stream_start_is_never_retried():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"one","choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("connection lost")

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=Broken())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = ChatProvider("fixture-key", client=client, max_retries=2)
            with pytest.raises(ProviderError) as error:
                async for _event in provider.complete(
                    {"model": "fixture", "messages": [{"role": "user", "content": "hello"}]}
                ):
                    pass
            assert error.value.code == "NETWORK_ERROR"
        assert len(requests) == 1

    asyncio.run(run())
