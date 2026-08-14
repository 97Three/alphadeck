import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import psycopg
import pytest

from alphadeck_platform.config import ROOT
from alphadeck_platform.migrations import migrate


@pytest.fixture(scope="module")
def url():
    value = os.getenv("PLATFORM_TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set PLATFORM_TEST_DATABASE_URL to an isolated empty test database")
    if not urlsplit(value).path.endswith("_test"):
        pytest.fail("Integration tests require a database name ending in _test")
    return value


def test_migrations_are_serialized_idempotent_and_checksum_guarded(url):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(migrate, [url, url]))
    expected = len(list((ROOT / "prisma/migrations").glob("*/migration.sql")))
    assert len(results[0]) + len(results[1]) in (0, expected)
    assert migrate(url) == []
    with psycopg.connect(url) as connection:
        count = connection.execute(
            'SELECT count(*) FROM "_prisma_migrations" WHERE finished_at IS NOT NULL'
        ).fetchone()[0]
        assert count == expected
        assert connection.execute("SELECT to_regclass('agent_missions')").fetchone()[0]


def test_migration_failure_rolls_back_ddl_and_history(url, tmp_path):
    directory = tmp_path / "migrations"
    shutil.copytree(ROOT / "prisma/migrations", directory)
    migration = directory / "20990101000000_intentional_failure"
    migration.mkdir()
    (migration / "migration.sql").write_text(
        "BEGIN;\nCREATE TABLE migration_failure_probe (id int);\nSELECT 1/0;\nCOMMIT;\n"
    )
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(url, directory)
    with psycopg.connect(url) as connection:
        assert (
            connection.execute("SELECT to_regclass('migration_failure_probe')").fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                'SELECT count(*) FROM "_prisma_migrations" WHERE migration_name=%s',
                (migration.name,),
            ).fetchone()[0]
            == 0
        )


def test_changed_migration_is_rejected(url, tmp_path):
    directory = tmp_path / "migrations"
    shutil.copytree(ROOT / "prisma/migrations", directory)
    script = sorted(directory.glob("*/migration.sql"))[0]
    script.write_bytes(script.read_bytes() + b"\n-- changed\n")
    with pytest.raises(RuntimeError, match="checksum changed"):
        migrate(url, directory)
