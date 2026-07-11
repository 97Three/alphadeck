"""Own benchmark subprocesses, database leases and cancellation in the Python worker."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from .config import ROOT, Settings
from .database import Database
from .evaluation_platform import (
    LOGS_PATH,
    REPORTS_PATH,
    EvaluationPlatform,
    benchmark_args,
    read_json,
)
from .lease_session import LeaseSession
from .scaffold import controlled_path


def belongs_to_claim(value, claim):
    if not isinstance(value, dict) or not isinstance(value.get("metadata"), dict):
        return False
    metadata = value["metadata"]
    queue, suite, runtime = (metadata.get(key) for key in ("queue", "suite", "runtime"))
    return (
        isinstance(queue, dict)
        and queue.get("id") == claim["item"]["id"]
        and queue.get("leaseToken") == claim["leaseToken"]
        and isinstance(suite, dict)
        and suite.get("mode") == claim["item"]["mode"]
        and isinstance(runtime, dict)
        and runtime.get("configuredModel") == claim["item"]["model"]
    )


async def stop_owned_process(process):
    def send(selected):
        with suppress(ProcessLookupError):
            if os.name != "nt":
                os.killpg(process.pid, selected)
            elif process.returncode is None:
                process.terminate() if selected == signal.SIGTERM else process.kill()

    send(signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), 3)
    except TimeoutError:
        pass
    finally:
        # Descendants may remain after their leader exits.
        send(signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
        await process.wait()


class EvaluationWorker:
    def __init__(
        self, database, *, root=ROOT, command_factory=None, heartbeat_ms=10000, ttl_ms=60000
    ):
        self.platform = EvaluationPlatform(database, root)
        self.queue, self.root = self.platform.queue, self.platform.root
        self.command_factory = command_factory
        self.heartbeat_ms, self.ttl_ms = heartbeat_ms, ttl_ms

    async def execute(self, claim, *, stop=None):
        stop = stop or asyncio.Event()
        process, log = None, None
        report_id = "report-" + str(uuid4().int)
        relative_log = (
            LOGS_PATH + "/" + claim["item"]["id"] + "-" + str(claim["attemptCount"]) + ".log"
        )
        try:
            if stop.is_set() or not await self.queue.heartbeat(claim):
                return
            log_path = controlled_path(self.root, relative_log)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = os.fdopen(
                os.open(
                    log_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                ),
                "ab",
            )
            environment = {
                **os.environ,
                "ALPHADECK_EVAL_REPORT_ID": report_id,
                "ALPHADECK_EVAL_QUEUE_ID": claim["item"]["id"],
                "ALPHADECK_EVAL_LEASE_TOKEN": claim["leaseToken"],
                "ALPHADECK_EVAL_ROOT": str(self.root),
            }
            command = (
                self.command_factory(claim, report_id)
                if self.command_factory
                else [sys.executable, *benchmark_args(claim["item"])]
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=ROOT / "services/platform-api",
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=os.name != "nt",
            )
            if not await self.queue.record_process(claim, process.pid, relative_log):
                return

            async def heartbeat():
                if not await self.queue.heartbeat(claim):
                    raise RuntimeError("评测任务已取消或租约已失效。")

            async def completion():
                waiting, stopping = (
                    asyncio.create_task(process.wait()),
                    asyncio.create_task(stop.wait()),
                )
                try:
                    finished, _ = await asyncio.wait(
                        (waiting, stopping), return_when=asyncio.FIRST_COMPLETED
                    )
                    if stopping in finished:
                        raise RuntimeError("评测 Worker 已停止。")
                    return await waiting
                finally:
                    stopping.cancel()
                    with suppress(asyncio.CancelledError):
                        await stopping
                    if not waiting.done():
                        waiting.cancel()
                    with suppress(asyncio.CancelledError):
                        await waiting

            async with LeaseSession(
                heartbeat, ttl_ms=self.ttl_ms, interval_ms=self.heartbeat_ms
            ) as lease:
                code = await lease.run(completion)
                raw = read_json(self.root, REPORTS_PATH + "/" + report_id + ".json")
                report = (
                    await self.platform.run(report_id) if belongs_to_claim(raw, claim) else None
                )
                passed = code == 0 and report is not None and report["passed"] is True
                committed = await self.queue.finish(
                    claim,
                    {
                        "status": "passed" if passed else "failed",
                        "exitCode": code,
                        "reportId": report["id"] if report else None,
                        "reportPath": report["filePath"] if report else None,
                        "error": None
                        if passed
                        else f"benchmark 退出码 {code}"
                        if code
                        else "评测报告未通过。"
                        if report
                        else "缺少属于本次任务的有效评测报告。",
                    },
                )
                if committed and report and not report["passed"]:
                    await self.platform.create_repairs(report)
        except asyncio.CancelledError:
            with suppress(Exception):
                await self.queue.finish(
                    claim, {"status": "failed", "exitCode": None, "error": "评测 Worker 已停止。"}
                )
            raise
        except Exception as error:
            with suppress(Exception):
                await self.queue.finish(
                    claim, {"status": "failed", "exitCode": None, "error": str(error)}
                )
        finally:
            if process:
                await stop_owned_process(process)
            if log:
                log.close()
            await self.queue.release_cancelled(claim)

    async def run(self, *, once=False, stop=None, poll_seconds=1):
        stop = stop or asyncio.Event()
        while not stop.is_set():
            await self.queue.check_schedule()
            claim = await self.queue.claim()
            if claim:
                await self.execute(claim, stop=stop)
            if once:
                return
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), poll_seconds)


async def main(once=False):
    settings = Settings.from_env()
    root = Path(os.getenv("ALPHADECK_EVAL_ROOT", str(ROOT))).resolve(strict=True)
    worker, stop = EvaluationWorker(Database(settings.database_url), root=root), asyncio.Event()
    loop = asyncio.get_running_loop()
    for selected in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected, stop.set)
        except NotImplementedError:
            signal.signal(selected, lambda *_: loop.call_soon_threadsafe(stop.set))
    logging.info("Python evaluation worker started")
    await worker.run(once=once, stop=stop)
    logging.info("Python evaluation worker stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main(parser.parse_args().once))
