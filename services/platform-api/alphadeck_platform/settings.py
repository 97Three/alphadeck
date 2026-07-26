import json
from contextlib import suppress

from psycopg.types.json import Jsonb

DEFAULT_MODEL = "local_qwen:qwen3.5-9b-q5km"
MODEL_ALIASES = {
    DEFAULT_MODEL: [DEFAULT_MODEL, "qwen3.5-9b-q5km", "qwen 3.5 9b", "local qwen"],
    "deepseek:deepseek-v4-flash": [
        "deepseek:deepseek-v4-flash",
        "modelport deepseek",
        "deepseek via modelport",
        "deepseek",
    ],
    "deepseek-v4-flash": [
        "deepseek-v4-flash",
        "deepseek direct",
        "deepseek official",
        "deepseek-official",
    ],
}


def normalize_model(value) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    for model, aliases in MODEL_ALIASES.items():
        if normalized in aliases:
            return model
    return DEFAULT_MODEL


def normalize_settings(value) -> dict:
    root = value if isinstance(value, dict) else {}
    cli = root.get("cli_settings", root.get("cliSettings"))
    pi = cli.get("pi") if isinstance(cli, dict) else None
    model = pi.get("model") if isinstance(pi, dict) else None
    return {"default_cli": "pi", "cli_settings": {"pi": {"model": normalize_model(model)}}}


class GlobalSettings:
    def __init__(self, database, data_dir):
        self.database, self.data_dir = database, data_dir

    async def load(self):
        row = await self.database.fetch_one(
            "SELECT value FROM platform_settings WHERE key='global'"
        )
        if row:
            return normalize_settings(row["value"])
        value = None
        legacy = self.data_dir / "global-settings.json"
        if legacy.is_file():
            with suppress(ValueError, OSError):
                value = json.loads(legacy.read_text(encoding="utf-8"))
        candidate = normalize_settings(value)
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO platform_settings (key,value,updated_at) VALUES ('global',%s,now())
                   ON CONFLICT (key) DO NOTHING""",
                (Jsonb(candidate),),
            )
            row = await (
                await connection.execute("SELECT value FROM platform_settings WHERE key='global'")
            ).fetchone()
        return normalize_settings(row["value"])

    async def update(self, value):
        current = await self.load()
        if isinstance(value, dict):
            cli = value.get("cli_settings", value.get("cliSettings"))
            if isinstance(cli, dict):
                current = normalize_settings({"cli_settings": cli})
        async with self.database.transaction() as connection:
            await connection.execute(
                """INSERT INTO platform_settings (key,value,updated_at) VALUES ('global',%s,now())
                   ON CONFLICT (key) DO UPDATE SET value=excluded.value,updated_at=now()""",
                (Jsonb(current),),
            )
        return current


def serialize(value):
    return {**value, "defaultCli": value["default_cli"], "cliSettings": value["cli_settings"]}
