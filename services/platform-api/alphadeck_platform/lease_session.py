import asyncio
from contextlib import suppress


class LeaseSession:
    """Keep an authority alive and stop its operation as soon as renewal fails."""

    def __init__(self, heartbeat, *, ttl_ms=120000, interval_ms=30000, release=None):
        if type(ttl_ms) is not int or type(interval_ms) is not int or not 0 < interval_ms < ttl_ms:
            raise ValueError("Heartbeat interval must be positive and smaller than its lease TTL")
        self.heartbeat, self.ttl, self.interval, self.release = (
            heartbeat,
            ttl_ms,
            interval_ms,
            release,
        )
        self.failure, self.fatal, self.task = asyncio.Event(), None, None
        self.deadline = None

    async def __aenter__(self):
        self.deadline = asyncio.get_running_loop().time() + self.ttl / 1000
        self.task = asyncio.create_task(self._heartbeat())
        return self

    def healthy(self):
        if self.fatal:
            raise self.fatal
        if self.deadline is not None and asyncio.get_running_loop().time() >= self.deadline:
            raise TimeoutError("Authoritative lease expired before it could be renewed")

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(self.interval / 1000)
                self.healthy()
                started = asyncio.get_running_loop().time()
                async with asyncio.timeout_at(self.deadline):
                    await self.heartbeat()
                # The database may have renewed near the start of the request.
                # Network latency must not extend the local authority window.
                self.deadline = started + self.ttl / 1000
        except asyncio.CancelledError:
            raise
        except Exception as cause:
            self.fatal = cause
            self.failure.set()

    async def run(self, operation):
        self.healthy()
        work, failed = asyncio.create_task(operation()), asyncio.create_task(self.failure.wait())
        try:
            done, _ = await asyncio.wait((work, failed), return_when=asyncio.FIRST_COMPLETED)
            self.healthy()
            if work in done:
                return await work
            raise RuntimeError("Lease renewal ended unexpectedly")
        finally:
            failed.cancel()
            if not work.done():
                work.cancel()
            with suppress(asyncio.CancelledError):
                await failed
            with suppress(asyncio.CancelledError, Exception):
                await work

    async def __aexit__(self, *_error):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.release:
            await self.release()
