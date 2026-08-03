import json
import logging
import os
import re
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from .config import ROOT
from .service_catalog import degradation_config
from .sessions import iso
from .skill_integrity import read_regular
from .workspace import write_atomic

RUN_FIELDS = {
    "id": "id",
    "templateId": "template_id",
    "scanId": "scan_id",
    "symbol": "symbol",
    "status": "status",
    "startedAt": "started_at",
    "completedAt": "completed_at",
    "total": "total",
    "succeeded": "succeeded",
    "failed": "failed",
    "bestResultId": "best_result_id",
    "objective": "objective",
    "source": "source",
    "results": "results",
}
JOB_FIELDS = {
    "id": "id",
    "templateId": "template_id",
    "scanId": "scan_id",
    "symbol": "symbol",
    "status": "status",
    "startedAt": "started_at",
    "completedAt": "completed_at",
    "runId": "run_id",
    "error": "error",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
}


def mapped(row, fields):
    return {
        key: iso(row[column]) if key.endswith("At") and row[column] is not None else row[column]
        for key, column in fields.items()
        if key not in ("startedAt", "completedAt") or row[column] is not None
    }


class StrategyScans:
    def __init__(self, database, root=None):
        self.database = database
        candidate = (
            Path(root)
            if root is not None
            else ROOT / os.getenv("STRATEGY_SCANS_DIR", "data/strategy-scans")
        )
        if candidate.is_symlink():
            raise ValueError("Strategy scans directory must not be a symbolic link")
        self.root = candidate.resolve()

    def legacy(self, kind):
        folder = self.root / kind
        if folder.is_symlink():
            raise ValueError("Unsafe strategy scan storage")
        result = []
        if folder.is_dir():
            for path in folder.glob("*.json"):
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}\.json", path.name):
                    continue
                try:
                    value = json.loads(read_regular(path, 8388608))
                    if isinstance(value, dict) and value.get("id") == path.stem:
                        result.append(value)
                except (OSError, ValueError):
                    pass
        return result

    async def list(self, kind):
        fields, table, order = (
            (RUN_FIELDS, "strategy_scan_runs", "completed_at")
            if kind == "runs"
            else (JOB_FIELDS, "strategy_scan_jobs", "created_at")
        )
        config = degradation_config()["components"]["database"]
        stored = []
        if config["enabled"]:
            try:
                rows = await self.database.fetch_all(
                    sql.SQL("SELECT * FROM {} ORDER BY {} DESC").format(
                        sql.Identifier(table), sql.Identifier(order)
                    )
                )
                stored = [mapped(row, fields) for row in rows]
            except Exception:
                if config["required"]:
                    raise
        merged = {item["id"]: item for item in self.legacy(kind)}
        merged.update({item["id"]: item for item in stored})
        return sorted(
            merged.values(),
            key=lambda item: item.get("completedAt" if kind == "runs" else "createdAt", ""),
            reverse=True,
        )

    async def save(self, kind, value):
        if not isinstance(value.get("id"), str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,160}", value["id"]
        ):
            raise ValueError("Invalid strategy scan ID")
        fields, table = (
            (RUN_FIELDS, "strategy_scan_runs")
            if kind == "runs"
            else (JOB_FIELDS, "strategy_scan_jobs")
        )
        config = degradation_config()["components"]["database"]
        if config["enabled"]:
            try:
                parameters = [
                    Jsonb(value.get(key, [])) if key == "results" else value.get(key)
                    for key in fields
                ]
                update = sql.SQL(",").join(
                    sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
                    for column in fields.values()
                    if column != "id"
                )
                async with self.database.transaction() as tx:
                    query = (
                        sql.SQL(
                            "INSERT INTO {} ({},updated_at) VALUES ({},clock_timestamp()) ON CONFLICT(id) DO UPDATE SET {},updated_at=clock_timestamp()"
                        )
                        if "updatedAt" not in fields
                        else sql.SQL(
                            "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT(id) DO UPDATE SET {}"
                        )
                    )
                    await tx.execute(
                        query.format(
                            sql.Identifier(table),
                            sql.SQL(",").join(map(sql.Identifier, fields.values())),
                            sql.SQL(",").join(sql.Placeholder() for _ in parameters),
                            update,
                        ),
                        parameters,
                    )
            except Exception:
                if config["required"]:
                    raise
        try:
            directory = self.root / kind
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink():
                raise ValueError("Unsafe scan projection directory")
            write_atomic(
                directory,
                value["id"] + ".json",
                (json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode(),
            )
        except (OSError, ValueError):
            logging.warning("Unable to write the optional strategy scan file projection")

    async def claim(self, identity):
        if not degradation_config()["components"]["database"]["enabled"]:
            return next(
                (
                    item
                    for item in self.legacy("jobs")
                    if item["id"] == identity and item["status"] == "queued"
                ),
                None,
            )
        async with self.database.transaction() as tx:
            row = await (
                await tx.execute(
                    "UPDATE strategy_scan_jobs SET status='running',started_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=%s AND status='queued' RETURNING *",
                    (identity,),
                )
            ).fetchone()
            return mapped(row, JOB_FIELDS) if row else None
