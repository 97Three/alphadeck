import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[3]


def market_base_url():
    return os.getenv("ALPHADECK_MARKET_API_URL", "http://127.0.0.1:8100").rstrip("/")


def load_environment(root: Path = ROOT) -> None:
    values = {}
    for name in (".env", ".env.local"):
        if (root / name).is_file():
            values.update(dotenv_values(root / name))
    for key, value in values.items():
        if value is not None:
            os.environ.setdefault(key, value)


def postgres_dsn(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("DATABASE_URL must point to PostgreSQL")
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "schema"]
    return urlunsplit(parsed._replace(query=urlencode(query)))


@dataclass(frozen=True)
class Settings:
    database_url: str
    projects_dir: Path
    market_url: str

    @classmethod
    def from_env(cls):
        load_environment()
        return cls(
            database_url=postgres_dsn(os.environ["DATABASE_URL"]),
            projects_dir=Path(os.getenv("PROJECTS_DIR", str(ROOT / "data/projects"))).resolve(),
            market_url=market_base_url(),
        )
