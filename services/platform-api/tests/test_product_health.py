import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from test_projects import project  # noqa: F401

from alphadeck_platform.evaluation_queue import date
from alphadeck_platform.product_health import first_research, product_dashboard, summarize
from alphadeck_platform.worker_capacity import WorkerRegistry
from alphadeck_platform.worker_observability import worker_dashboard

FIXTURES = json.loads((Path(__file__).parent / "fixtures/product-health.json").read_text())


@pytest.mark.parametrize("fixture", FIXTURES)
def test_original_product_and_first_research_metrics(fixture):
    if fixture["kind"] == "product":
        actual = summarize(fixture["requests"], 3, date(fixture["now"]))
    else:
        actual = first_research(
            fixture["firstRequests"],
            fixture["acceptedRequests"],
            date(fixture["since"]),
            date(fixture["now"]),
        )
    assert actual == fixture["expected"]


def test_native_product_and_worker_metrics_read_database(project):  # noqa: F811
    async def run():
        database, pool = project["database"], "test." + uuid4().hex
        registry = WorkerRegistry(database)
        worker = await registry.register(1, 2, 60000, pool=pool)
        try:
            data = await product_dashboard(database)
            assert data["available"], data["error"]
            workers = await worker_dashboard(database, pool=pool)
            assert workers["available"] and workers["summary"]["activeWorkers"] == 1
            assert workers["summary"]["globalCapacity"] == 2
            assert workers["summary"]["processCapacity"] == 1
        finally:
            await registry.stop(worker)
            async with database.transaction() as tx:
                await tx.execute("DELETE FROM agent_worker_instances WHERE pool_key=%s", (pool,))

    asyncio.run(run())
