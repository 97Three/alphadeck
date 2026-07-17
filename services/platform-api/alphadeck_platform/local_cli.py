import argparse
import asyncio
import json

from .access_bootstrap import ensure_access_control
from .config import Settings
from .database import Database
from .migrations import migrate


async def main(command):
    settings = Settings.from_env()
    database = Database(settings.database_url)
    if command == "db-ready":
        try:
            await database.fetch_one("SELECT 1")
        except Exception:
            return 1
        return 0
    if command == "migrate":
        applied = await asyncio.to_thread(migrate, settings.database_url)
        print(f"Applied {len(applied)} migrations")
    print(json.dumps(await ensure_access_control(database), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("db-ready", "migrate", "ensure-access-control"))
    raise SystemExit(asyncio.run(main(parser.parse_args().command)))
