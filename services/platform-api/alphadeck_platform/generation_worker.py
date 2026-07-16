"""Native durable dispatcher; production domain handlers are registered explicitly."""

import asyncio
import logging
from contextlib import suppress

from .dispatch import Dispatch, DispatchError
from .lease_session import LeaseSession
from .maintenance import reconcile_usage
from .mission_store import Missions
from .quota import QuotaError
from .user_requests import UserRequests
from .worker_capacity import WorkerCapacity, WorkerError, WorkerRegistry

BUSY = {
    "GENERATION_DISPATCH_BUSY",
    "GENERATION_PROJECT_BUSY",
    "GENERATION_DISPATCH_CONFLICT",
    "GENERATION_DISPATCH_NOT_AVAILABLE",
    "GENERATION_DISPATCH_TERMINAL",
    "GENERATION_DISPATCH_CANCELLED",
    "GENERATION_DISPATCH_ATTEMPTS_EXHAUSTED",
}


class GenerationWorker:
    def __init__(
        self,
        database,
        runtime,
        *,
        process_concurrency=1,
        global_concurrency=None,
        pool="generation.default",
        ttl_ms=120000,
        heartbeat_ms=30000,
        batch_size=20,
        preview_manager=None,
    ):
        global_concurrency = (
            process_concurrency if global_concurrency is None else global_concurrency
        )
        if (
            type(process_concurrency) is not int
            or type(global_concurrency) is not int
            or not (
                1 <= process_concurrency <= 16 and process_concurrency <= global_concurrency <= 256
            )
        ):
            raise ValueError("Invalid worker concurrency")
        if (
            type(batch_size) is not int
            or not 1 <= batch_size <= 200
            or not 0 < heartbeat_ms < ttl_ms
        ):
            raise ValueError("Invalid worker batch or heartbeat configuration")
        if not callable(getattr(runtime, "execute", None)):
            raise ValueError("Generation worker requires a registered native domain runtime")
        self.database, self.runtime, self.previews = database, runtime, preview_manager
        self.concurrency, self.capacity, self.pool = process_concurrency, global_concurrency, pool
        self.ttl, self.interval, self.batch = ttl_ms, heartbeat_ms, batch_size
        self.dispatch, self.slots, self.registry = (
            Dispatch(database),
            WorkerCapacity(database),
            WorkerRegistry(database),
        )
        self.registration = None

    async def execute_job(self, job, owner):
        capacity = await self.slots.claim(
            self.capacity, str(job["id"]), owner, self.ttl, pool=self.pool
        )
        if not capacity:
            return False
        fence = None
        try:

            async def release():
                with suppress(WorkerError):
                    await self.slots.release(capacity)

            async with LeaseSession(
                lambda: self.slots.heartbeat(capacity, self.ttl),
                ttl_ms=self.ttl,
                interval_ms=self.interval,
                release=release,
            ) as slot:
                fence = await self.dispatch.claim(
                    job["project_id"], job["request_id"], owner, self.ttl
                )
                async with LeaseSession(
                    lambda: self.dispatch.heartbeat(fence, self.ttl),
                    ttl_ms=self.ttl,
                    interval_ms=self.interval,
                ) as dispatch:
                    claimed = await self.dispatch.read_claimed(fence)

                    async def execute():
                        await self.runtime.execute(
                            {
                                "jobId": str(job["id"]),
                                "projectId": job["project_id"],
                                "requestId": job["request_id"],
                                "selectedModel": claimed["selected_model"],
                                "cliPreference": claimed["cli_preference"],
                                "executionEnvelope": claimed["execution_envelope"],
                            },
                            fence=fence,
                        )

                    await slot.run(lambda: dispatch.run(execute))
                    current = await self.dispatch.get(job["project_id"], job["request_id"])
                    if not current or current["status"] == "running":
                        raise WorkerError(
                            "GENERATION_HANDLER_INCOMPLETE",
                            "Generation handler returned without committing a terminal job state.",
                        )
                slot.healthy()
                return True
        except QuotaError:
            if fence is not None:
                await self._fail(job, fence)
                return True
            return False
        except DispatchError as cause:
            if cause.code in BUSY:
                return False
            await self._fail(job, fence)
            return fence is not None
        except asyncio.CancelledError:
            # Lease loss/shutdown leaves durable state for the reconciler; no unowned write follows.
            raise
        except Exception:
            logging.exception("Native generation worker failed for job %s", job["id"])
            await self._fail(job, fence)
            return fence is not None

    async def _fail(self, job, fence):
        if fence is None:
            return
        try:
            final = await self.dispatch.finish(
                job["project_id"],
                job["request_id"],
                "failed",
                fence=fence,
                code="GENERATION_WORKER_FAILED",
                message="生成 worker 执行失败，请检查运行记录后重新发起。",
            )
            if final["status"] == "failed":
                mission = await Missions(self.database).read(job["project_id"], job["request_id"])
                if mission:
                    await Missions(self.database).terminal(
                        mission, "failed", "GENERATION_WORKER_FAILED", "生成 worker 执行失败。"
                    )
                await UserRequests(self.database).status(
                    job["project_id"], job["request_id"], "failed", "生成 worker 执行失败。"
                )
        except DispatchError:
            # A replaced worker cannot finalize the newer owner's attempt.
            logging.warning("Generation worker failure could not be projected after lease loss")

    async def tick(self, owner):
        if self.previews:
            await self.previews.cleanup_deleted()
        await self.dispatch.reconcile(limit=self.batch)
        jobs = await self.dispatch.claimable(self.batch)
        completed = 0
        for index in range(0, len(jobs), self.concurrency):
            completed += sum(
                await asyncio.gather(
                    *(
                        self.execute_job(job, owner)
                        for job in jobs[index : index + self.concurrency]
                    )
                )
            )
        return completed

    async def run(self, *, once=False, stop=None, poll_seconds=1, maintenance_seconds=30):
        stop = stop or asyncio.Event()
        if poll_seconds <= 0 or maintenance_seconds <= 0:
            raise ValueError("Worker poll and maintenance intervals must be positive")
        self.registration = await self.registry.register(
            self.concurrency, self.capacity, self.ttl, pool=self.pool
        )
        owner = "worker-instance:" + self.registration["id"]

        async def release():
            if self.previews:
                await self.previews.cleanup_all()
            await self.registry.stop(self.registration)

        next_maintenance = 0
        async with LeaseSession(
            lambda: self.registry.heartbeat(self.registration, self.ttl),
            ttl_ms=self.ttl,
            interval_ms=self.interval,
            release=release,
        ) as registration:
            while not stop.is_set():
                if asyncio.get_running_loop().time() >= next_maintenance:
                    await reconcile_usage(self.database)
                    next_maintenance = asyncio.get_running_loop().time() + maintenance_seconds
                cycle = asyncio.create_task(registration.run(lambda: self.tick(owner)))
                stopping = asyncio.create_task(stop.wait())
                try:
                    await asyncio.wait({cycle, stopping}, return_when=asyncio.FIRST_COMPLETED)
                    if stop.is_set():
                        cycle.cancel()
                        await asyncio.gather(cycle, return_exceptions=True)
                        break
                    processed = await cycle
                finally:
                    if not cycle.done():
                        cycle.cancel()
                        await asyncio.gather(cycle, return_exceptions=True)
                    stopping.cancel()
                    await asyncio.gather(stopping, return_exceptions=True)
                if once:
                    break
                if not processed:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), poll_seconds)
