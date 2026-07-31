"""Explicit import of legacy local state; regular startup never calls this module."""

import argparse
import asyncio
import json
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from .config import ROOT, Settings
from .database import Database
from .evaluation_platform import REPAIRS_PATH, REPORTS_PATH, TICKET_COLUMNS, EvaluationPlatform
from .evaluation_queue import QUEUE_FIELDS, EvaluationQueue, date, virtual_item
from .runtime_store import snake
from .scaffold import controlled_path
from .sessions import iso
from .skill_integrity import read_regular
from .skill_source import strict_json
from .strategy_scans import StrategyScans


def read(root, relative, fallback=None):
    try:
        return strict_json(read_regular(controlled_path(root, relative), 33554432))
    except FileNotFoundError:
        return fallback


async def upsert(tx, table, values, *, key="id", overwrite=True):
    columns = list(values)
    conflict = (
        sql.SQL("DO UPDATE SET ")
        + sql.SQL(",").join(
            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
            for column in columns
            if column != key
        )
        if overwrite
        else sql.SQL("DO NOTHING")
    )
    result = await tx.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) ").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, columns)),
            sql.SQL(",").join(sql.Placeholder() for _ in columns),
            sql.Identifier(key),
        )
        + conflict,
        tuple(values.values()),
    )
    return result.rowcount


async def sync_workspaces(database, root):
    root = Path(root).resolve(strict=True)
    created = skipped = 0
    for directory in sorted(root.iterdir()):
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or not re.fullmatch(r"project-[A-Za-z0-9_-]{1,120}", directory.name)
        ):
            continue
        workspace = read(directory, ".data-agent/workspace.json", {})
        profile = read(directory, ".data-agent/profile.json", {})
        generation = read(directory, ".data-agent/generation-state.json", {})
        package = read(directory, "package.json", {})
        now = datetime.now(UTC)
        created_at = date(workspace.get("createdAt"))
        match = re.search(r"(?:project|continuation)-(\d{10,})", directory.name)
        if created_at is None and match:
            numeric = int(match[1])
            with suppress(ValueError, OverflowError, OSError):
                created_at = datetime.fromtimestamp(
                    numeric / 1000 if numeric > 9999999999 else numeric, UTC
                )
        updated_at = date(generation.get("updatedAt")) or datetime.fromtimestamp(
            directory.stat().st_mtime, UTC
        )
        composition = workspace.get("composition") or profile.get("composition") or {}
        agent_profile = composition.get("profile") or {}
        values = {
            "id": directory.name,
            "name": workspace.get("projectName") or package.get("name") or directory.name,
            "description": str(generation.get("originalInstruction") or "")[:240] or None,
            "status": "failed" if generation.get("status") == "failed" else "idle",
            "repo_path": str(directory),
            "initial_prompt": generation.get("originalInstruction") or "",
            "template_type": "nextjs",
            "preferred_cli": "pi",
            "selected_model": (workspace.get("runtime") or {}).get("modelId")
            or generation.get("selectedModel"),
            "settings": json.dumps(
                {
                    "quant": {
                        "capabilityId": profile.get("selectedCapabilityId") or "stock_diagnosis",
                        "status": "recovered_from_workspace",
                    }
                }
            ),
            "created_at": created_at or now,
            "updated_at": updated_at,
            "last_active_at": updated_at,
            "agent_profile_id": agent_profile.get("id"),
            "agent_profile_version": agent_profile.get("version"),
            "data_agent_composition_sha256": composition.get("sha256"),
        }
        for key in ("agent_profile_id", "agent_profile_version", "data_agent_composition_sha256"):
            if values[key] is None:
                del values[key]
        async with database.transaction() as tx:
            count = await upsert(tx, "projects", values, overwrite=False)
        created += count
        skipped += not count
    return {"created": created, "skipped": skipped}


async def migrate_platform_state(database, root=ROOT):
    root = Path(root).resolve(strict=True)
    result = {
        "settings": 0,
        "scanRuns": 0,
        "scanJobs": 0,
        "evalRuns": 0,
        "evalQueue": 0,
        "evalRepairs": 0,
        "evalSchedule": 0,
    }
    settings = read(root, "data/global-settings.json")
    if isinstance(settings, dict):
        async with database.transaction() as tx:
            result["settings"] = await upsert(
                tx,
                "platform_settings",
                {"key": "global", "value": Jsonb(settings), "updated_at": datetime.now(UTC)},
                key="key",
                overwrite=False,
            )
    scan_root = root / "data/strategy-scans"
    scans = StrategyScans(database, scan_root)
    for kind, label in (("runs", "scanRuns"), ("jobs", "scanJobs")):
        for value in scans.legacy(kind):
            await scans.save(kind, value)
            result[label] += 1
    platform, queue = EvaluationPlatform(database, root), EvaluationQueue(database)
    reports = controlled_path(root, REPORTS_PATH)
    for path in sorted(reports.glob("report-*.json")):
        if re.fullmatch(r"report-\d+\.json", path.name):
            await platform.run(path.stem)
            result["evalRuns"] += 1
    legacy_queue = read(root, "tmp/alphadeck-eval-queue/queue.json", [])
    for raw in legacy_queue if isinstance(legacy_queue, list) else []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        item = {
            **virtual_item(raw),
            **{key: raw[key] for key in QUEUE_FIELDS if key in raw},
            "cli": "pi",
        }
        # A legacy PID is not proof of ownership of a current Python child process.
        if item["status"] == "running":
            item.update(
                status="failed",
                pid=None,
                finishedAt=iso(datetime.now(UTC)),
                error="Imported legacy running job has no valid Python Worker lease; create a new evaluation",
            )
        values = {snake(key): item[key] for key in QUEUE_FIELDS}
        for key in ("created_at", "started_at", "finished_at"):
            values[key] = date(values[key])
        values.update(selected_cases=Jsonb(values["selected_cases"]), updated_at=datetime.now(UTC))
        async with database.transaction() as tx:
            result["evalQueue"] += await upsert(tx, "eval_queue_items", values, overwrite=False)
    repairs = read(root, REPAIRS_PATH, [])
    for raw in repairs if isinstance(repairs, list) else []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        ticket = platform.ticket(raw)
        values = {
            snake(key): Jsonb(ticket[key])
            if key in ("failures", "validationSummaries", "suggestedActions", "skillVersions")
            else date(ticket[key])
            if key.endswith("At")
            else ticket[key]
            for key in TICKET_COLUMNS
        }
        async with database.transaction() as tx:
            result["evalRepairs"] += await upsert(tx, "eval_repair_tickets", values)
    schedule = read(root, "tmp/alphadeck-eval-queue/schedule.json")
    if isinstance(schedule, dict):
        async with database.transaction() as tx:
            if not await (
                await tx.execute("SELECT id FROM eval_schedules WHERE id='default'")
            ).fetchone():
                current = await queue.read_schedule(tx)
                await queue.write_schedule(tx, {**current, **schedule, "cli": "pi"})
                result["evalSchedule"] = 1
    return result


async def main(command):
    settings = Settings.from_env()
    database = Database(settings.database_url)
    value = (
        await sync_workspaces(database, settings.projects_dir)
        if command == "workspaces"
        else await migrate_platform_state(database)
    )
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("workspaces", "platform"))
    asyncio.run(main(parser.parse_args().command))
