import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .finance_values import js_string
from .icu_collation import collator
from .scaffold import controlled_path
from .sessions import iso
from .skill_catalog import SkillCatalogue, now, valid_skill
from .skill_integrity import hash_directory, read_regular
from .skill_source import agent_metadata, editable, source_files

ORDER = {"instruction": 0, "reference": 1, "script": 2, "agent": 3, "asset": 4, "other": 5}
SCOPES = ("workflow", "quant", "input", "evidence", "platform", "visualization")


def shortened(value):
    return value[:10] + "..." + value[-8:] if value else None


def kind(relative, *, directory=False):
    if relative == "SKILL.md" and not directory:
        return "instruction"
    for prefix, value in (
        ("references", "reference"),
        ("scripts", "script"),
        ("assets", "asset"),
        ("agents", "agent"),
    ):
        if relative.startswith(prefix + "/") or directory and relative == prefix:
            return value
    return "other"


def file_time(path):
    return iso(datetime.fromtimestamp(path.stat().st_mtime, UTC))


def strings(value):
    return [js_string(item) for item in value] if isinstance(value, list) else []


def json_object(root, path, fallback=None):
    try:
        value = json.loads(read_regular(controlled_path(root, path), 2097152))
        if not isinstance(value, dict):
            raise ValueError("Skills metadata must be an object")
        return value
    except FileNotFoundError:
        if fallback is None:
            raise
        return fallback


def dashboard(root):
    root = Path(root).resolve(strict=True)
    registry = json_object(root, ".pi/skills.registry.json")
    lock = json_object(root, ".pi/skills.lock.json", {"schemaVersion": 1, "skills": {}})
    changelog = json_object(root, ".pi/skills.changelog.json", {"schemaVersion": 1, "skills": {}})
    policy = registry.get("policy") or {}
    package_dir = policy.get("packageDir", ".pi/skill-packages").replace("\\", "/")
    if not package_dir or Path(package_dir).is_absolute() or ".." in package_dir.split("/"):
        package_dir = ".pi/skill-packages"
    result = []
    for definition in registry["coreSkills"]:
        identity = definition["id"]
        if not valid_skill(identity):
            raise ValueError("Invalid Skill ID")
        directory = controlled_path(root, ".pi/skills/" + identity)
        files = source_files(directory)
        directories = [path for path in directory.rglob("*") if path.is_dir()]
        source_hash = hash_directory(directory)
        source_entries = []
        for path in files:
            relative = path.relative_to(directory).as_posix()
            content = read_regular(path, 4194304)
            digest = hashlib.sha256(content).hexdigest()
            source_entries.append(
                {
                    "path": relative,
                    "name": path.name,
                    "kind": kind(relative),
                    "editable": editable(relative),
                    "size": len(content),
                    "updatedAt": file_time(path),
                    "sha256": digest,
                    "sha256Short": shortened(digest),
                }
            )
        directory_entries = [
            {
                "path": path.relative_to(directory).as_posix(),
                "name": path.name,
                "kind": kind(path.relative_to(directory).as_posix(), directory=True),
                "fileCount": sum(path in child.parents for child in files),
                "updatedAt": file_time(path),
            }
            for path in directories
        ]
        for entries in (source_entries, directory_entries):
            entries.sort(
                key=lambda entry: (ORDER[entry["kind"]], collator("en-US").key(entry["path"]))
            )
        expected = lock.get("skills", {}).get(identity)
        lock_entry = expected or {}
        package_path = package_dir + "/" + identity + ".tgz"
        target = controlled_path(root, package_path)
        exists = target.is_file()
        packed = read_regular(target, 5242880) if exists else None
        package_hash = hashlib.sha256(packed).hexdigest() if packed is not None else None
        releases = []
        for release in changelog.get("skills", {}).get(identity, {}).get("releases", []):
            snapshot = package_dir + "/versions/" + identity + "/" + release["version"] + ".tgz"
            releases.append(
                {
                    **release,
                    "snapshot": {
                        "exists": controlled_path(root, snapshot).is_file(),
                        "packagePath": snapshot,
                    },
                }
            )
        current = next(
            (release for release in releases if release["version"] == definition["version"]), None
        )
        source_changed = bool(
            lock_entry.get("sourceSha256") and lock_entry["sourceSha256"] != source_hash["hash"]
        )
        package_changed = bool(
            lock_entry.get("packageSha256")
            and package_hash
            and lock_entry["packageSha256"] != package_hash
        )
        mismatch = bool(
            lock_entry.get("version") and lock_entry["version"] != definition["version"]
        )
        relative_files = {entry["path"] for entry in source_entries}
        references = [
            path
            for path in relative_files
            if path.startswith("references/") and path.endswith(".md")
        ]
        scripts = [
            path
            for path in relative_files
            if path.startswith("scripts/") and re.search(r"\.(?:py|js|mjs|sh)$", path)
        ]
        skill = (
            read_regular(directory / "SKILL.md", 4194304).decode("utf-8", "replace")
            if "SKILL.md" in relative_files
            else ""
        )
        missing = []
        if "SKILL.md" not in relative_files:
            missing.append("SKILL.md")
        dir_names = {entry["path"] for entry in directory_entries}
        missing.extend(
            name + "/" for name in ("references", "scripts", "agents") if name not in dir_names
        )
        if not references:
            missing.append("references/*.md")
        if not scripts:
            missing.append("scripts/*")
        if not current:
            missing.append("changelog")
        elif not current["snapshot"]["exists"]:
            missing.append("current_snapshot")
        if expected is None:
            missing.append("lock")
        else:
            missing.extend(
                "lock." + key
                for key in ("version", "sourceSha256", "packageSha256")
                if not lock_entry.get(key)
            )
            if type(lock_entry.get("fileCount")) is not int:
                missing.append("lock.fileCount")
            if lock_entry.get("packagePath") != package_path:
                missing.append("lock.packagePath")
        if not exists:
            missing.append("package")
        for key, prefix in (("scripts", "script:"), ("references", "reference:")):
            missing.extend(
                prefix + path for path in strings(definition.get(key)) if path not in relative_files
            )
        for paths, key, markdown in ((scripts, "scripts", False), (references, "references", True)):
            # Source dashboard traverses files in kind/path order.
            for path in sorted(paths, key=collator("en-US").key):
                if path not in strings(definition.get(key)):
                    missing.append("registry:" + path)
                if ("](" + path + ")" if markdown else path) not in skill:
                    missing.append("navigation:" + path)
        if "agents/openai.yaml" not in relative_files:
            missing.append("agents/openai.yaml")
        else:
            try:
                agent_metadata(
                    read_regular(directory / "agents/openai.yaml", 524288).decode(),
                    identity,
                    quoted=False,
                )
            except Exception:
                missing.append("agents/openai.yaml:invalid")
        missing.extend(
            label
            for label, condition in (
                ("source_hash", source_changed),
                ("package_hash", package_changed),
                ("version_lock", mismatch),
            )
            if condition
        )
        health = (
            "error"
            if any(item not in ("source_hash", "package_hash") for item in missing)
            else "warning"
            if missing
            else "ok"
        )
        scope = definition.get("scope") if definition.get("scope") in SCOPES else "workflow"
        result.append(
            {
                **definition,
                "scope": scope,
                **{
                    key: strings(definition.get(key))
                    for key in (
                        "inputs",
                        "outputs",
                        "scripts",
                        "references",
                        "endpoints",
                        "validation",
                    )
                },
                "changelog": {
                    "currentRelease": current,
                    "releases": releases,
                    "latestVersion": releases[0]["version"] if releases else None,
                    "releaseCount": len(releases),
                },
                "lock": {
                    "version": lock_entry.get("version"),
                    "packagePath": lock_entry.get("packagePath") or package_path,
                    "sourceSha256": lock_entry.get("sourceSha256"),
                    "packageSha256": lock_entry.get("packageSha256"),
                    "fileCount": lock_entry.get("fileCount"),
                },
                "source": {
                    "path": ".pi/skills/" + identity,
                    "skillFilePath": ".pi/skills/" + identity + "/SKILL.md",
                    "fileCount": source_hash["fileCount"],
                    "directoryCount": len(directories),
                    "editableFileCount": sum(entry["editable"] for entry in source_entries),
                    "referenceFileCount": sum(
                        entry["kind"] == "reference" for entry in source_entries
                    ),
                    "scriptFileCount": sum(entry["kind"] == "script" for entry in source_entries),
                    "assetFileCount": sum(entry["kind"] == "asset" for entry in source_entries),
                    "sourceSha256": source_hash["hash"],
                    "sourceSha256Short": shortened(source_hash["hash"]),
                    **{
                        "has" + name.capitalize(): name in dir_names
                        for name in ("scripts", "references", "assets", "agents")
                    },
                    "directories": directory_entries,
                    "files": source_entries,
                },
                "package": {
                    "exists": exists,
                    "path": package_path,
                    "size": len(packed) if packed is not None else 0,
                    "updatedAt": file_time(target) if exists else None,
                    "packageSha256": package_hash,
                    "packageSha256Short": shortened(package_hash),
                },
                "health": {
                    "status": health,
                    "missing": missing,
                    "sourceChanged": source_changed,
                    "packageChanged": package_changed,
                    "versionMismatch": mismatch,
                },
            }
        )
    totals = {
        "total": len(result),
        **{
            status: sum(entry["health"]["status"] == status for entry in result)
            for status in ("ok", "warning", "error")
        },
        **{
            status: sum(entry["status"] == status for entry in result)
            for status in ("stable", "planned")
        },
        "scopes": {scope: sum(entry["scope"] == scope for entry in result) for scope in SCOPES},
    }
    return {
        "generatedAt": now(),
        "policy": policy,
        "totals": totals,
        "skills": result,
        "lock": {
            "schemaVersion": lock.get("schemaVersion", 1),
            "packageFormat": lock.get("packageFormat") or str(policy.get("packageFormat", "tgz")),
        },
    }


def active_dashboard(root):
    catalogue = SkillCatalogue(root)
    state = catalogue.read()
    return dashboard(catalogue.resolve(state["active"]) if state["active"] else root)
