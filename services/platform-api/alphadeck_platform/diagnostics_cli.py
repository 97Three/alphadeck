import argparse
import asyncio
import json
import sys

from .audit import report
from .config import Settings, load_environment
from .database import Database
from .database_cli import doctor
from .models import availability
from .readiness import web_readiness


async def main(command, full=False):
    load_environment()
    if command == "models":
        result = await availability()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["available"] else 1
    settings = Settings.from_env()
    database = Database(settings.database_url)
    result = await web_readiness(database, settings.projects_dir)
    if command == "doctor":
        try:
            result["database"] = await doctor(database)
        except Exception as error:
            result["database"] = {"error": str(error)}
            result["ok"] = False
        result["python"] = sys.version.split()[0]
        from app import create_app

        result["migration"] = report(create_app(database=database))
        if full:
            result["models"] = await availability()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("models", "doctor", "integrations"))
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.command, args.full)))
