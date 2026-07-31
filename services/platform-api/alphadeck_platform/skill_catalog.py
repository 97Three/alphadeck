import hashlib
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from .config import ROOT
from .resource_lock import resource_lock, sync_directory
from .skill_integrity import read_regular
from .workspace import write_atomic

CONTENT = (
    ".pi/skills",
    ".pi/skill-packages",
    ".pi/skills.registry.json",
    ".pi/skills.lock.json",
    ".pi/skills.changelog.json",
    "config/pi-agent-skill-capsules.json",
)
TARGETS = {
    "pi-agent": (".pi", "installed-skills.json", "PI Agent"),
    "claude-code": (".claude", "alphadeck-installed-skills.json", "Claude Code"),
    "codex": (".agents", "alphadeck-installed-skills.json", "Codex"),
}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def checksum(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def now():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def valid_skill(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value) is not None


def bounded(value, maximum):
    return isinstance(value, str) and 0 < len(value) <= maximum


def timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def shape(value, required, optional=()):
    return (
        isinstance(value, dict)
        and set(required) <= value.keys()
        and value.keys() <= set(required) | set(optional)
    )


def validate_state(state):
    if (
        not shape(
            state,
            (
                "schemaVersion",
                "generation",
                "active",
                "drafts",
                "releases",
                "deployments",
                "events",
            ),
        )
        or type(state["schemaVersion"]) is not int
        or state["schemaVersion"] != 1
        or type(state["generation"]) is not int
        or state["generation"] < 0
        or (state["active"] is not None and not valid_digest(state["active"]))
    ):
        raise ValueError("Invalid skill catalogue state")
    if not isinstance(state["drafts"], dict) or not isinstance(state["deployments"], dict):
        raise ValueError("Invalid skill catalogue maps")
    for identity, draft in state["drafts"].items():
        if (
            not valid_skill(identity)
            or not shape(draft, ("revision", "base", "actor", "updatedAt"))
            or not valid_digest(draft["revision"])
            or not valid_digest(draft["base"])
            or not bounded(draft["actor"], 256)
            or not timestamp(draft["updatedAt"])
        ):
            raise ValueError("Invalid skill draft")
    if not isinstance(state["releases"], list) or not isinstance(state["events"], list):
        raise ValueError("Invalid skill catalogue history")
    for release in state["releases"]:
        if (
            not shape(release, ("skillId", "version", "revision", "actor", "date"))
            or not valid_skill(release["skillId"])
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release["version"])
            or not valid_digest(release["revision"])
            or not bounded(release["actor"], 256)
            or not timestamp(release["date"])
        ):
            raise ValueError("Invalid skill release")
    for key, deployment in state["deployments"].items():
        if (
            not re.fullmatch(r"(pi-agent|claude-code|codex):[a-f0-9]{64}", key)
            or not shape(
                deployment,
                ("id", "revision", "skillIds", "target", "updatedAt", "actor"),
                ("previous",),
            )
            or not uuid(deployment["id"])
            or not valid_digest(deployment["revision"])
            or deployment["target"] not in TARGETS
            or not timestamp(deployment["updatedAt"])
            or not bounded(deployment["actor"], 256)
        ):
            raise ValueError("Invalid Skill deployment")
        for item in [deployment, *([deployment["previous"]] if "previous" in deployment else [])]:
            if item is not deployment and not shape(item, ("revision", "skillIds")):
                raise ValueError("Invalid previous Skill deployment")
            identities = item["skillIds"]
            if (
                not valid_digest(item["revision"])
                or not isinstance(identities, list)
                or len(identities) > 200
                or len(set(identities)) != len(identities)
                or not all(valid_skill(identity) for identity in identities)
            ):
                raise ValueError("Invalid deployment Skill set")
    for event in state["events"]:
        if (
            not shape(event, ("id", "action", "actor", "at"), ("skillId", "version", "target"))
            or not uuid(event["id"])
            or not bounded(event["action"], 80)
            or not bounded(event["actor"], 256)
            or not timestamp(event["at"])
            or ("skillId" in event and not valid_skill(event["skillId"]))
            or ("version" in event and not bounded(event["version"], 32))
            or ("target" in event and not bounded(event["target"], 128))
        ):
            raise ValueError("Invalid Skill event")
    return state


def directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Unsafe skill state directory")
    return path


def inventory(root):
    result, total, entries = {}, 0, 0

    def visit(relative, depth):
        nonlocal total, entries
        entries += 1
        if entries > 10000 or depth > 18:
            raise ValueError("Skill catalogue exceeds entry limit")
        path = root / relative
        if path.is_symlink():
            raise ValueError("Skill catalogue must not contain symlinks")
        if path.is_dir():
            result[relative + "/"] = checksum("directory")
            for child in sorted(path.iterdir()):
                if child.name != ".DS_Store":
                    visit(relative + "/" + child.name, depth + 1)
        else:
            content = read_regular(path, 10 * 1024 * 1024)
            total += len(content)
            if total > 100 * 1024 * 1024:
                raise ValueError("Skill catalogue exceeds byte limit")
            result[relative] = checksum(content)

    for relative in CONTENT:
        visit(relative, 0)
    return result


class SkillCatalogue:
    def __init__(self, root=ROOT):
        self.root = root.resolve()
        configured = (
            os.getenv("ALPHADECK_SKILLS_STATE_DIR", "").strip()
            if self.root == ROOT.resolve()
            else ""
        )
        self.directory = Path(os.path.abspath(self.root / (configured or "data/skill-catalog")))

    def ensure(self):
        directory(self.directory.parent)
        return directory(self.directory)

    def read(self):
        for path in (self.directory.parent, self.directory):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError("Unsafe skill state directory")
        try:
            state = json.loads(read_regular(self.directory / "state.json", 8 * 1024 * 1024))
        except FileNotFoundError:
            return {
                "schemaVersion": 1,
                "generation": 0,
                "active": None,
                "drafts": {},
                "releases": [],
                "deployments": {},
                "events": [],
            }
        return validate_state(state)

    def commit(self, state):
        next_state = validate_state(deepcopy(state))
        next_state["generation"] += 1
        content = (json.dumps(next_state, ensure_ascii=False, indent=2) + "\n").encode()
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("Skill catalogue state exceeds size limit")
        write_atomic(self.ensure(), "state.json", content)
        sync_directory(self.directory)

    def lock(self):
        return resource_lock(
            self.ensure(),
            {"purpose": "other", "operationId": "skill-catalog"},
            recover_dead_local_owner=True,
        )

    def resolve(self, revision, *, verify_all=False):
        if not valid_digest(revision):
            raise ValueError("Invalid Skill catalogue revision")
        path = self.directory / "images" / revision
        for parent in (self.directory, path.parent, path):
            if parent.is_symlink() or not parent.is_dir():
                raise ValueError("Unsafe Skill catalogue image")
        raw = read_regular(path / "manifest.json", 2 * 1024 * 1024)
        if checksum(raw) != revision:
            raise ValueError("Skill catalogue manifest integrity check failed")
        manifest = json.loads(raw)
        if manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("Invalid Skill catalogue manifest")
        for relative in (item for item in CONTENT if item.endswith(".json")):
            if checksum(read_regular(path / relative, 2 * 1024 * 1024)) != manifest["files"].get(
                relative
            ):
                raise ValueError("Skill catalogue metadata integrity check failed")
        if verify_all and compact(inventory(path)) != compact(manifest["files"]):
            raise ValueError("Skill catalogue content integrity check failed")
        return path

    def stage(self, source):
        inventory(source)
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.ensure()))
        try:
            for relative in CONTENT:
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if (source / relative).is_dir():
                    shutil.copytree(source / relative, target)
                else:
                    shutil.copy2(source / relative, target)
            return stage
        except BaseException:
            shutil.rmtree(stage)
            raise

    def seal(self, stage):
        if stage.parent != self.directory or stage.is_symlink():
            raise ValueError("Invalid Skill catalogue stage")
        files = inventory(stage)
        manifest = compact({"schemaVersion": 1, "files": files}).encode()
        revision = checksum(manifest)
        images = directory(self.ensure() / "images")
        write_atomic(stage, "manifest.json", manifest)
        destination = images / revision
        if destination.exists():
            self.resolve(revision, verify_all=True)
            shutil.rmtree(stage)
        else:
            stage.rename(destination)
            sync_directory(images)
        return revision

    def initialize(self, state):
        if state["active"]:
            return
        stage = self.stage(self.root)
        try:
            state["active"] = self.seal(stage)
            registry = json.loads(
                read_regular(self.root / ".pi/skills.registry.json", 2 * 1024 * 1024)
            )
            state["releases"].extend(
                {
                    "skillId": entry["id"],
                    "version": entry["version"],
                    "revision": state["active"],
                    "actor": "repository",
                    "date": now(),
                }
                for entry in registry["coreSkills"]
            )
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    @staticmethod
    def deployment_key(workspace, target):
        return target + ":" + checksum(str(Path(workspace).resolve()))

    def runtime(self, workspace=None, target="pi-agent"):
        state = self.read()
        deployment = (
            state["deployments"].get(self.deployment_key(workspace, target)) if workspace else None
        )
        revision = deployment["revision"] if deployment else state["active"]
        return self.resolve(revision) if revision else self.root, deployment

    @staticmethod
    def event(state, action, actor, **fields):
        state["events"].append(
            {
                "id": str(uuid4()),
                "action": action,
                "actor": actor,
                "at": now(),
                **{key: value for key, value in fields.items() if value is not None},
            }
        )
