import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_projects import project  # noqa: F401

from alphadeck_platform.operations_api import infrastructure_health
from alphadeck_platform.readiness import probe_redis, run_probes, web_readiness
from alphadeck_platform.service_catalog import (
    degradation_config,
    dependency_edges,
    resolved_catalog,
    validate_catalog,
)

CASES = json.loads((Path(__file__).parent / "fixtures/operations-catalog.json").read_text())


@pytest.mark.parametrize("case", CASES)
def test_configuration_matches_original_typescript(case):
    import copy

    expected = copy.deepcopy(case["services"])
    for service in expected:
        if service.get("endpointSource") == "default":
            for key in ("endpoint", "healthUrl"):
                if isinstance(service.get(key), str):
                    for old, new in (
                        (":3000", ":3100"),
                        (":8000", ":8100"),
                        (":35433", ":35434"),
                        (":36380", ":36381"),
                    ):
                        service[key] = service[key].replace(old, new)
    assert resolved_catalog(case["env"]) == expected
    assert validate_catalog(case["env"]) == case["validation"]
    assert dependency_edges(case["services"]) == case["dependencies"]
    assert degradation_config(case["env"]) == case["degradation"]


def test_readiness_required_optional_and_disabled_semantics():
    async def run():
        calls = []

        async def fail():
            calls.append("called")
            raise ValueError("synthetic-secret-not-exposed")

        value = await run_probes(
            [("disabled", False, False, fail), ("optional", True, False, fail)],
            checked_at=datetime(2026, 11, 3, tzinfo=UTC),
        )
        assert value["ok"] and len(calls) == 1
        assert value["components"][0]["status"] == "disabled"
        assert value["components"][1]["status"] == "failed"
        assert "synthetic-secret" not in json.dumps(value)
        value = await run_probes([("required", True, True, fail)])
        assert not value["ok"]

    asyncio.run(run())


def test_readiness_checks_real_database_and_workspace_but_optional_network_can_fail(project):  # noqa: F811
    async def run():
        def unavailable(request):
            return httpx.Response(503, json={})

        value = await web_readiness(
            project["database"],
            project["root"],
            env={"NODE_ENV": "test", "ALPHADECK_REDIS_CACHE_ENABLED": "0"},
            transport=httpx.MockTransport(unavailable),
        )
        assert value["ok"]
        by_name = {item["name"]: item for item in value["components"]}
        assert by_name["database"]["ok"] and by_name["workspace"]["ok"]
        assert by_name["marketApi"]["status"] == "failed"
        strict = await web_readiness(
            project["database"],
            project["root"],
            env={
                "NODE_ENV": "test",
                "ALPHADECK_DEGRADATION_MODE": "strict",
                "ALPHADECK_REDIS_CACHE_ENABLED": "0",
            },
            transport=httpx.MockTransport(unavailable),
        )
        assert not strict["ok"]

    asyncio.run(run())


def test_infrastructure_masks_password_and_inspects_actual_postgres(project, monkeypatch):  # noqa: F811
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://fixture:synthetic-password@localhost:1234/fixture_test"
    )

    async def run():
        value, status = await infrastructure_health(
            project["database"], docker={"available": False, "running": False, "service": None}
        )
        assert status == 200 and value["data"]["connected"]
        assert "synthetic-password" not in json.dumps(value)
        assert value["data"]["timescale"] == {"enabled": False, "version": None}

    asyncio.run(run())


def test_redis_probe_auth_select_ping_and_auth_failure():
    async def run():
        commands = []

        async def handle(reader, writer):
            try:
                for _ in range(3):
                    count = int((await reader.readline())[1:])
                    command = []
                    for _ in range(count):
                        size = int((await reader.readline())[1:])
                        command.append((await reader.readexactly(size)).decode())
                        await reader.readexactly(2)
                    commands.append(command)
                    writer.write(b"+OK\r\n")
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            await probe_redis(f"redis://user:synthetic@127.0.0.1:{port}/2")
        assert commands == [["AUTH", "user", "synthetic"], ["SELECT", "2"], ["PING"]]

        async def denied(reader, writer):
            await reader.read(1024)
            writer.write(b"-NOAUTH Authentication required\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(denied, "127.0.0.1", 0)
        async with server:
            with pytest.raises(ValueError, match="Redis command failed"):
                await probe_redis(f"redis://127.0.0.1:{server.sockets[0].getsockname()[1]}/0")

    asyncio.run(run())
