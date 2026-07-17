"""Git subprocess adapter; credentials are never stored in remotes or command arguments."""

import asyncio
import base64
import os
from contextlib import suppress

from .resource_lock import resource_lock
from .scaffold import controlled_path
from .workspace import write_atomic

IGNORES = """node_modules/
.next/
out/
dist/
build/
.turbo/
.env
.env.*
.DS_Store
.git-backup-*
.vercel/
.pi-workspace.lock/
.pi-workspace.lock.*
npm-debug.log*
yarn-debug.log*
yarn-error.log*
pnpm-debug.log*
""".splitlines()


class GitFailure(Exception):
    pass


class GitRepository:
    async def run(self, root, *args, credentials=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1")
        if credentials:
            encoded = base64.b64encode((credentials[0] + ":" + credentials[1]).encode()).decode()
            env.update(
                GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_0="Authorization: Basic " + encoded,
            )
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
            cwd=root,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=180)
        except BaseException:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            # Git diagnostics may include credentials or private repository data.
            raise GitFailure("Git operation failed: " + args[0])
        return stdout.decode("utf-8", "replace").strip()

    async def initialize(self, root, user):
        controlled_path(root, ".git")
        if not (root / ".git").exists():
            await self.run(root, "init")
        target = controlled_path(root, ".gitignore")
        existing = target.read_text() if target.exists() else ""
        missing = [line for line in IGNORES if line not in existing.splitlines()]
        if missing:
            write_atomic(
                root, ".gitignore", (existing.rstrip() + "\n" + "\n".join(missing) + "\n").encode()
            )
        await self.run(root, "config", "--local", "user.name", user.get("name") or user["login"])
        await self.run(
            root,
            "config",
            "--local",
            "user.email",
            user.get("email") or user["login"] + "@users.noreply.github.com",
        )

    async def commit(self, root, message):
        ignored = await self.run(root, "ls-files", "-ci", "--exclude-standard", "-z")
        for name in ignored.split("\0"):
            if name:
                await self.run(root, "rm", "--cached", "--ignore-unmatch", "--", name)
        await self.run(root, "add", "-A")
        if not await self.run(root, "status", "--porcelain"):
            return False
        await self.run(root, "commit", "-m", message)
        return True

    async def connect(self, root, user, url):
        async with resource_lock(
            root, {"purpose": "github_connection"}, recover_dead_local_owner=True
        ):
            await self.initialize(root, user)
            try:
                await self.run(root, "rev-parse", "HEAD")
            except GitFailure:
                await self.run(root, "commit", "--allow-empty", "-m", "Initial commit")
            await self.run(root, "branch", "-M", "main")
            remotes = (await self.run(root, "remote")).splitlines()
            await self.run(
                root,
                "remote",
                *(["set-url", "origin", url] if "origin" in remotes else ["add", "origin", url]),
            )
            await self.commit(root, "Initial commit - connected to GitHub")

    async def push(self, root, user, data, token):
        async with resource_lock(root, {"purpose": "github_push"}, recover_dead_local_owner=True):
            await self.initialize(root, user)
            if not await self.commit(root, "Update from AlphaDeck"):
                return False
            # Preserve remote history on a rejected push; callers can resolve divergence explicitly.
            await self.run(
                root,
                "push",
                "-u",
                data["clone_url"],
                data.get("default_branch") or "main",
                credentials=(data["owner"], token),
            )
            return True
