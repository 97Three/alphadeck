import asyncio
import json
from pathlib import Path

import pytest

from alphadeck_platform.query_rewrite import rewrite_failure, rewrite_query
from alphadeck_platform.research_time import (
    ResearchTimeError,
    constrain_historical_query,
    research_time_for_plan,
    resolve_research_time,
)
from alphadeck_platform.semantic_rewriter import rewrite_with_provider

FIXTURE = json.loads((Path(__file__).parent / "fixtures/query-rewrite.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_query_rewrite_semantic_guards_and_symbol_resolution_match_original(case):
    async def semantic(_input):
        return case["outcome"]

    async def resolver(query, _count):
        if case.get("resolverError"):
            raise RuntimeError("private resolver error")
        return case["rows"].get(query, {"results": []})

    result = asyncio.run(
        rewrite_query(case["query"], case["options"], semantic_rewriter=semantic, resolver=resolver)
    )
    if result["execution"]["llm"]["durationMs"] is not None:
        result["execution"]["llm"]["durationMs"] = 0
    assert result == case["expected"]
    if result["status"] == "failed":
        assert rewrite_failure(result)


@pytest.mark.parametrize("case", FIXTURE["temporal"])
def test_historical_research_time_matches_original_and_excludes_partial_daily_bar(case):
    if "error" in case["expected"]:
        with pytest.raises(ResearchTimeError) as error:
            resolve_research_time(case["input"], FIXTURE["now"])
        assert {"code": error.value.code, "message": str(error.value)} == case["expected"]["error"]
    else:
        result = resolve_research_time(case["input"], FIXTURE["now"])
        assert result == case["expected"]["result"]
        if result:
            assert research_time_for_plan({"researchTime": result}, FIXTURE["now"]) == result


@pytest.mark.parametrize("case", FIXTURE["adapters"], ids=lambda case: case["name"])
def test_structured_semantic_model_protocol_matches_original(case):
    class Provider:
        name = "fixture"
        request = None

        async def complete(self, request):
            self.request = request
            yield {
                "type": "tool_call_delta",
                "index": 0,
                "nameDelta": "emit_query_rewrite_semantics",
                "argumentsDelta": json.dumps(case["payload"], ensure_ascii=False),
            }
            yield {
                "type": "usage",
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
            }

    provider = Provider()
    result = asyncio.run(rewrite_with_provider(case["input"], provider))
    assert result == case["expected"]
    assert provider.request == case["request"]


def test_historical_request_cannot_escape_cutoff_or_use_realtime_data():
    temporal = resolve_research_time(
        {
            "unit": "date_range",
            "startDate": "2025-06-01",
            "endDate": "2025-06-30",
            "asOf": "2025-06-30T12:00:00+08:00",
        },
        FIXTURE["now"],
    )
    assert temporal["endDate"] == "2025-06-29"
    assert constrain_historical_query("/api/v1/quotes/history/600519", {}, temporal) == {
        "start": "20250601",
        "end": "20250629",
        "period": "daily",
        "adjustment": "none",
    }
    for path, query in (
        ("/api/v1/quotes/realtime/600519", {}),
        ("/api/v1/quotes/history/600519", {"end": "20260101"}),
        ("/api/v1/quotes/history/600519", {"adjustment": "qfq"}),
    ):
        with pytest.raises(ResearchTimeError):
            constrain_historical_query(path, query, temporal)
