import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest

from alphadeck_platform.data_prefetch import prefetch_data
from alphadeck_platform.prefetch_transport import MarketTransport, WorkspaceArtifacts

CASES = json.loads((Path(__file__).parent / "fixtures/data-prefetch.json").read_text())


def normalize(value):
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "<generated>"
            if key in {"generatedAt", "created_at", "createdAt", "resolvedAt", "updatedAt"}
            else normalize(item)
            for key, item in value.items()
        }
    return value


def fixture_transport(case, calls):
    async def upstream(request):
        path = request.url.path
        calls.append(
            {
                "path": path,
                "query": dict(request.url.params),
                "method": request.method,
                **({"body": json.loads(request.content)} if request.content else {}),
            }
        )
        response = next(row for row in case["responses"] if row["request"] == calls[-1])
        return httpx.Response(response["status"], json=response["payload"])

    return httpx.MockTransport(upstream)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["scenario"]["name"])
def test_full_prefetch_matches_original_and_writes_under_lease(case, tmp_path, monkeypatch):
    for module in ("baseline_evidence", "prefetch_market", "data_prefetch"):
        monkeypatch.setattr(
            "alphadeck_platform." + module + ".timestamp", lambda: "2026-11-03T00:00:00.000Z"
        )

    async def run():
        mutations, calls, progress = [], [], []

        async def authority(operation):
            mutations.append(True)
            return await operation(None)

        artifacts = WorkspaceArtifacts(tmp_path, authority)
        plan = copy.deepcopy(case["plan"])
        await artifacts.write(".data-agent/finance-run-plan.json", plan)

        async def report(value):
            progress.append(value)

        async with httpx.AsyncClient(transport=fixture_transport(case, calls)) as client:
            result = await prefetch_data(
                plan,
                MarketTransport(client=client, base_url="http://fixture"),
                artifacts,
                on_progress=report,
            )
        entries = {path: artifacts.read(path) for path in case["expected"]["entries"]}
        actual = normalize({"outcome": result, "entries": entries})
        assert actual == case["expected"], differences(actual, case["expected"])[:20]
        assert sorted(calls, key=lambda value: json.dumps(value, sort_keys=True)) == sorted(
            case["calls"], key=lambda value: json.dumps(value, sort_keys=True)
        )
        assert len(mutations) >= len(entries)
        if progress:
            assert (
                progress[-1]["completed"] == len(result.get("symbols", [])) + progress[-1]["failed"]
            )

    asyncio.run(run())


def differences(left, right, path=""):
    if type(left) is not type(right) and not (
        isinstance(left, (float, int)) and isinstance(right, (float, int))
    ):
        return [(path, left, right)]
    if isinstance(left, dict):
        return [(path + "/" + key, "missing/extra") for key in left.keys() ^ right.keys()] + [
            item
            for key in left.keys() & right.keys()
            for item in differences(left[key], right[key], path + "/" + key)
        ]
    if isinstance(left, list):
        if len(left) != len(right):
            return [(path + "/length", len(left), len(right))]
        return [
            item
            for index, (a, b) in enumerate(zip(left, right, strict=True))
            for item in differences(a, b, path + "/" + str(index))
        ]
    return [] if left == right else [(path, left, right)]


def test_cancelled_generation_does_not_write_after_http_finishes(tmp_path):
    async def run():
        active = True

        async def authority(operation):
            if not active:
                raise RuntimeError("Lease lost")
            return await operation(None)

        async def check():
            if not active:
                raise RuntimeError("Lease lost")

        async def upstream(request):
            nonlocal active
            active = False
            return httpx.Response(200, json={"symbol": "600519", "price": 120})

        artifacts = WorkspaceArtifacts(tmp_path, authority)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            with pytest.raises(RuntimeError, match="Lease lost"):
                await prefetch_data(
                    {
                        "runId": "cancelled",
                        "capabilityId": "stock_diagnosis",
                        "symbols": ["600519"],
                        "question": "分析",
                        "dataRequirements": [],
                    },
                    MarketTransport(client=client, base_url="http://fixture"),
                    artifacts,
                    assert_active=check,
                )
        assert not (tmp_path / "data_file").exists()

    asyncio.run(run())
