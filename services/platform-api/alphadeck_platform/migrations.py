"""Apply the original ordered PostgreSQL migrations without a Node runtime."""

import hashlib
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import ROOT, postgres_dsn


def migrate(url: str, directory: Path = ROOT / "prisma/migrations") -> list[str]:
    scripts = sorted(directory.glob("*/migration.sql"))
    if not scripts:
        raise ValueError("No versioned migrations found")
    applied = []
    with psycopg.connect(postgres_dsn(url), autocommit=True, row_factory=dict_row) as connection:
        # Share the upstream migration lock so Node and Python deployments cannot race.
        connection.execute("SELECT pg_advisory_lock(72707369)")
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS "_prisma_migrations" (
                id VARCHAR(36) PRIMARY KEY, checksum VARCHAR(64) NOT NULL,
                finished_at TIMESTAMPTZ, migration_name VARCHAR(255) NOT NULL,
                logs TEXT, rolled_back_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                applied_steps_count INTEGER NOT NULL DEFAULT 0)""")
            history = connection.execute(
                'SELECT * FROM "_prisma_migrations" WHERE rolled_back_at IS NULL'
            ).fetchall()
            if any(row["finished_at"] is None for row in history):
                raise RuntimeError("An unfinished migration requires recovery before startup")
            completed = {row["migration_name"]: row for row in history}
            names = {script.parent.name for script in scripts}
            if completed.keys() - names:
                raise RuntimeError("Database migration history is newer than this checkout")
            for script in scripts:
                name = script.parent.name
                content = script.read_bytes()
                checksum = hashlib.sha256(content).hexdigest()
                existing = completed.get(name)
                if existing:
                    if existing["checksum"] != checksum:
                        raise RuntimeError(f"Applied migration checksum changed: {name}")
                    continue
                # DDL and its success receipt commit together. Errors roll back both.
                with connection.transaction():
                    sql = re.sub(r"(?m)^(?:BEGIN|COMMIT);\s*$", "", content.decode("utf-8-sig"))
                    connection.execute(sql, prepare=False)
                    connection.execute(
                        """INSERT INTO "_prisma_migrations"
                           (id, checksum, migration_name, finished_at, applied_steps_count)
                           VALUES (%s,%s,%s,%s,1)""",
                        (str(uuid.uuid4()), checksum, name, datetime.now(UTC)),
                    )
                applied.append(name)
        finally:
            connection.execute("SELECT pg_advisory_unlock(72707369)")
    return applied


if __name__ == "__main__":
    from .config import Settings

    print(f"Applied {len(migrate(Settings.from_env().database_url))} migrations")
