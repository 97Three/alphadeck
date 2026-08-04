"""Native process entry point; no Node business modules are imported or launched."""

import argparse
import asyncio
import logging
import os
import signal

from .config import Settings
from .database import Database
from .finance_generation import FinanceGenerationRuntime
from .generation_worker import GenerationWorker
from .preview import PreviewManager
from .projects import Projects


def integer(name, fallback, maximum):
    try:
        value = int(os.getenv(name, str(fallback)))
    except ValueError as cause:
        raise ValueError(name + " must be a positive integer.") from cause
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}.")
    return value


async def main(once=False):
    settings = Settings.from_env()
    os.environ["PI_AGENT_DISPATCH_MODE"] = "worker"
    database = Database(settings.database_url)
    projects = Projects(database, settings.projects_dir)
    previews = PreviewManager(projects)
    concurrency = integer("PI_AGENT_WORKER_CONCURRENCY", 1, 16)
    worker = GenerationWorker(
        database,
        FinanceGenerationRuntime(database, projects, previews).registry(),
        process_concurrency=concurrency,
        global_concurrency=integer("PI_AGENT_WORKER_GLOBAL_CONCURRENCY", concurrency, 256),
        ttl_ms=integer("PI_AGENT_WORKER_SLOT_LEASE_TTL_MS", 120000, 86400000),
        heartbeat_ms=integer("PI_AGENT_WORKER_SLOT_HEARTBEAT_INTERVAL_MS", 30000, 86400000),
        batch_size=integer("PI_AGENT_WORKER_CLAIM_BATCH_SIZE", 20, 200),
        preview_manager=previews,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for selected in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected, stop.set)
        except NotImplementedError:
            signal.signal(selected, lambda *_: loop.call_soon_threadsafe(stop.set))
    logging.info("Python generation worker started")
    await worker.run(
        once=once,
        stop=stop,
        poll_seconds=integer("PI_AGENT_WORKER_POLL_INTERVAL_MS", 1000, 60000) / 1000,
    )
    logging.info("Python generation worker stopped")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main(arguments.once))
