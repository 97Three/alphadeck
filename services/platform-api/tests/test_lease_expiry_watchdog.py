import asyncio

import pytest

from alphadeck_platform.lease_session import LeaseSession


def test_blocked_database_heartbeat_cancels_owned_operation_before_more_work():
    async def run():
        cancelled = asyncio.Event()

        async def heartbeat():
            await asyncio.Event().wait()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with LeaseSession(heartbeat, ttl_ms=80, interval_ms=10) as lease:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(lease.run(operation), 0.5)
            assert cancelled.is_set()

    asyncio.run(run())


def test_local_expiry_is_checked_after_event_loop_was_delayed():
    async def run():
        async def heartbeat():
            return None

        async with LeaseSession(heartbeat, ttl_ms=100, interval_ms=50) as lease:
            lease.deadline = asyncio.get_running_loop().time() - 1
            with pytest.raises(TimeoutError):
                lease.healthy()

    asyncio.run(run())
