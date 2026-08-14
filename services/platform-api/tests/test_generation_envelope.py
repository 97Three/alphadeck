import asyncio
import json
from pathlib import Path

import pytest

from alphadeck_platform.generation_envelope import (
    GenerationRuntimeRegistry,
    parse_generation_envelope,
)

CASES = json.loads((Path(__file__).parent / "fixtures/generation-envelope.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_durable_generation_envelope_matches_original(case):
    if "error" in case:
        with pytest.raises(ValueError) as error:
            parse_generation_envelope(case["input"])
        assert str(error.value) == case["error"]
    else:
        assert parse_generation_envelope(case["input"]) == case["expected"]


def test_registry_routes_only_registered_profiles_and_preserves_dispatch_fence():
    async def run():
        registry = GenerationRuntimeRegistry()
        job = {
            "executionEnvelope": CASES[0]["input"],
            "projectId": "project-1",
            "requestId": "request-1",
        }
        with pytest.raises(ValueError, match="No generation handler"):
            await registry.execute(job)
        received = []

        async def handler(value, *, fence):
            received.append((value, fence))

        registry.register("sales.studio", handler)
        with pytest.raises(ValueError, match="Duplicate"):
            registry.register("sales.studio", handler)
        await registry.execute(job, fence={"jobId": "job-1"})
        assert received[0][0] == job and received[0][1] == {"jobId": "job-1"}

    asyncio.run(run())
