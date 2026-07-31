"""Verify a relocated Vue build is served by the native Python API."""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import httpx

from .auth import Authorization
from .config import ROOT
from .database import Database


def inspect_artifact(artifact: Path) -> list[str]:
    index = artifact / "index.html"
    assets = artifact / "assets"
    if not index.is_file() or not assets.is_dir():
        raise ValueError("Vue artifact is missing index.html or assets")
    if any(path.name == ".env" or path.name.startswith(".env.") for path in artifact.rglob("*")):
        raise ValueError("Vue artifact contains environment files")
    for path in artifact.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(artifact):
            raise ValueError("Vue artifact has an escaping link: " + str(path))
    names = [path.relative_to(artifact).as_posix() for path in assets.rglob("*") if path.is_file()]
    if not any(name.endswith(".js") for name in names) or not any(name.endswith(".css") for name in names):
        raise ValueError("Vue artifact is missing compiled JavaScript or CSS")
    return names


async def main():
    from app import create_app

    artifact = ROOT / "web" / "dist"
    inspect_artifact(artifact)
    with tempfile.TemporaryDirectory(prefix="alphadeck-vue-standalone-") as folder:
        isolated = Path(folder)
        relocated = isolated / "web"
        shutil.copytree(artifact, relocated)
        asset_names = inspect_artifact(relocated)
        database = Database("postgresql://unused:unused@127.0.0.1:1/standalone_test")
        app = create_app(
            database=database,
            authorization=Authorization(database, enabled=False, secret=""),
            data_dir=isolated / "data",
            projects_dir=isolated / "projects",
            web_dist_dir=relocated,
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            health = await client.get("/api/health")
            homepage = await client.get("/")
            deep_link = await client.get("/research-reports")
            asset = await client.get("/" + next(name for name in asset_names if name.endswith(".js")))
            missing_api = await client.get("/api/not-real")
        assert health.status_code == 200 and health.json()["ok"] is True
        assert homepage.status_code == 200 and "AlphaDeck" in homepage.text
        assert deep_link.status_code == 200 and deep_link.text == homepage.text
        assert asset.status_code == 200 and asset.headers["cache-control"].startswith("public")
        assert missing_api.status_code == 404
        for header in ("content-security-policy", "referrer-policy", "x-content-type-options", "x-frame-options"):
            assert health.headers.get(header), header
    print(json.dumps({"passed": True, "relocatedVueArtifact": True, "homepage": 200, "assets": True, "securityHeaders": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
