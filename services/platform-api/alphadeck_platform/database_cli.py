"""Native database setup and diagnostics; never imports or executes a Node ORM."""

import argparse
import asyncio
import hashlib
import json
import re

import psycopg
from psycopg.rows import dict_row

from .access_bootstrap import ensure_access_control
from .config import ROOT, Settings, postgres_dsn
from .database import Database
from .migrations import migrate


def initialize(url, directory=ROOT / "sqls"):
    scripts = sorted(path for path in directory.glob("*.sql") if re.match(r"\d+-", path.name))
    if not scripts:
        raise ValueError("No quantitative database bootstrap SQL found")
    applied = []
    with psycopg.connect(postgres_dsn(url), autocommit=True, row_factory=dict_row) as connection:
        connection.execute("SELECT pg_advisory_lock(72707370)")
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS _alphadeck_bootstrap (
                name text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())""")
            for path in scripts:
                content = path.read_bytes()
                checksum = hashlib.sha256(content).hexdigest()
                row = connection.execute(
                    "SELECT checksum FROM _alphadeck_bootstrap WHERE name=%s", (path.name,)
                ).fetchone()
                if row:
                    if row["checksum"] != checksum:
                        raise RuntimeError("Applied bootstrap SQL changed: " + path.name)
                    continue
                with connection.transaction():
                    connection.execute(content.decode("utf-8-sig"), prepare=False)
                    connection.execute(
                        "INSERT INTO _alphadeck_bootstrap (name,checksum) VALUES (%s,%s)",
                        (path.name, checksum),
                    )
                applied.append(path.name)
        finally:
            connection.execute("SELECT pg_advisory_unlock(72707370)")
    return {"bootstrap": applied, "migrations": migrate(url)}


async def doctor(database):
    identity = await database.fetch_one(
        "SELECT current_database() AS database, current_user AS user"
    )
    extension = await database.fetch_one(
        "SELECT extversion FROM pg_extension WHERE extname='timescaledb'"
    )
    if not extension:
        raise RuntimeError(
            "TimescaleDB extension is not installed; use the project's TimescaleDB container and run db:init"
        )
    tables = await database.fetch_all(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='quant' ORDER BY table_name"
    )
    await database.fetch_one("SELECT id FROM projects LIMIT 1")
    return {
        **identity,
        "timescaledb": extension["extversion"],
        "quantTables": [row["table_name"] for row in tables],
        "applicationTables": "reachable",
    }


async def main(command):
    settings = Settings.from_env()
    database = Database(settings.database_url)
    if command == "init":
        result = await asyncio.to_thread(initialize, settings.database_url)
        result["accessControl"] = await ensure_access_control(database)
    else:
        result = await doctor(database)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "doctor"))
    asyncio.run(main(parser.parse_args().command))
