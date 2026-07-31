import inspect
import json
import shutil
from pathlib import Path

from .resource_lock import resource_lock, sync_directory
from .skill_catalog import TARGETS, now, valid_digest, valid_skill
from .skill_integrity import hash_directory, read_regular
from .workspace import write_atomic


def exists(path):
    return path.exists() or path.is_symlink()


def ensure_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Skill installation directory must be a real directory")


def remove(path, root):
    if path.parent != root or path == root:
        raise ValueError("Skill cleanup path is outside its transaction root")
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        if path.resolve().parent != root.resolve():
            raise ValueError("Unsafe Skill cleanup path")
        shutil.rmtree(path)


async def call(callback, *arguments):
    result = callback(*arguments)
    return await result if inspect.isawaitable(result) else result


def read_receipt(workspace, target):
    directory, name, label = TARGETS[target]
    runtime = workspace / directory
    if not runtime.exists():
        return None
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("Unsafe Skill runtime directory")
    try:
        value = json.loads(read_regular(runtime / name, 1024 * 1024))
    except FileNotFoundError:
        return None
    if (
        value.get("schemaVersion") != 1
        or value.get("runtime") != label
        or value.get("skillsDirectory") != directory + "/skills"
        or not isinstance(value.get("skills"), dict)
    ):
        raise ValueError("Invalid Skill installation receipt")
    for identity, claim in value["skills"].items():
        if (
            not valid_skill(identity)
            or not isinstance(claim, dict)
            or not valid_digest(claim.get("sourceSha256"))
        ):
            raise ValueError("Invalid Skill installation claim")
    return value


async def recover(runtime, receipt_name, is_activated=None):
    journal = runtime / ".skill-install-transaction"
    if not exists(journal):
        return
    if journal.is_symlink() or not journal.is_dir():
        raise ValueError("Unsafe Skill install journal")
    intent_path = journal / "intent.json"
    if not exists(intent_path):
        remove(journal, runtime)
        return
    intent = json.loads(read_regular(intent_path, 4096))
    if type(intent.get("hadSkills")) is not bool or type(intent.get("hadReceipt")) is not bool:
        raise ValueError("Invalid Skill install journal")
    committed = exists(journal / "committed")
    if "activationId" in intent:
        if not isinstance(intent["activationId"], str) or is_activated is None:
            raise ValueError("Skill recovery requires the platform activation record")
        committed = await call(is_activated, intent["activationId"])
    if not committed:
        for name, backup, had in (
            ("skills", "previous-skills", intent["hadSkills"]),
            (receipt_name, "previous-receipt", intent["hadReceipt"]),
        ):
            saved, destination = journal / backup, runtime / name
            if exists(saved):
                if saved.is_symlink():
                    raise ValueError("Unsafe Skill recovery backup")
                remove(destination, runtime)
                saved.rename(destination)
            elif not had:
                remove(destination, runtime)
    remove(journal, runtime)
    sync_directory(runtime)


async def install_assets(
    workspace,
    target,
    assets,
    *,
    capability_id=None,
    overwrite_modified=False,
    trusted_managed=None,
    activation=None,
):
    if target not in TARGETS:
        raise ValueError("Unsupported Skill target")
    identities = [asset["id"] for asset in assets]
    if len(set(identities)) != len(identities) or not all(
        valid_skill(identity) for identity in identities
    ):
        raise ValueError("Invalid Skill installation IDs")
    workspace = Path(workspace).resolve(strict=True)
    directory, receipt_name, label = TARGETS[target]
    runtime = workspace / directory
    ensure_directory(runtime)
    async with resource_lock(
        runtime, {"purpose": "other", "operationId": "skill-install"}, recover_dead_local_owner=True
    ):
        is_activated = activation["isCommitted"] if activation else None
        await recover(runtime, receipt_name, is_activated)
        try:
            receipt = read_receipt(workspace, target)
        except ValueError:
            if trusted_managed is None or not overwrite_modified:
                raise
            receipt = None
        destination = runtime / "skills"
        previous = exists(destination)
        if previous and (destination.is_symlink() or not destination.is_dir()):
            raise ValueError("Unsafe Skill installation directory")
        claims = (
            trusted_managed if trusted_managed is not None else (receipt or {}).get("skills", {})
        )
        names = [entry.name for entry in destination.iterdir()] if previous else []
        for identity in identities:
            if identity in names and identity not in claims:
                raise ValueError(f"Refusing to overwrite unmanaged Skill: {identity}")
        for identity, claim in claims.items():
            if not valid_skill(identity):
                raise ValueError("Invalid managed Skill identity")
            if (
                identity in names
                and hash_directory(destination / identity)["hash"] != claim["sourceSha256"]
                and not overwrite_modified
            ):
                raise ValueError(f"Locally modified Skill requires explicit overwrite: {identity}")
        journal = runtime / ".skill-install-transaction"
        journal.mkdir(mode=0o700)
        try:
            staged = journal / "next-skills"
            staged.mkdir()
            for name in names:
                if name not in claims:
                    source = destination / name
                    if source.is_dir() and not source.is_symlink():
                        shutil.copytree(source, staged / name, symlinks=True)
                    elif source.is_symlink():
                        (staged / name).symlink_to(
                            source.readlink(), target_is_directory=source.is_dir()
                        )
                    else:
                        shutil.copy2(source, staged / name, follow_symlinks=False)
            for asset in assets:
                target_path = staged / asset["id"]
                await call(asset["prepare"], target_path)
                if hash_directory(target_path)["hash"] != asset["claim"]["sourceSha256"]:
                    raise ValueError(f"Installed Skill content failed verification: {asset['id']}")
            next_receipt = {
                "schemaVersion": 1,
                "runtime": label,
                "installedAt": now(),
                "capabilityId": capability_id,
                "skillsDirectory": directory + "/skills",
                "skills": {asset["id"]: asset["claim"] for asset in assets},
            }
            write_atomic(
                journal,
                "next-receipt",
                (json.dumps(next_receipt, ensure_ascii=False, indent=2) + "\n").encode(),
            )
            had_receipt = exists(runtime / receipt_name)
            intent = {"hadSkills": previous, "hadReceipt": had_receipt}
            if activation:
                intent["activationId"] = activation["id"]
            write_atomic(journal, "intent.json", json.dumps(intent).encode())
            sync_directory(journal)
            if previous:
                destination.rename(journal / "previous-skills")
            staged.rename(destination)
            if had_receipt:
                (runtime / receipt_name).rename(journal / "previous-receipt")
            (journal / "next-receipt").rename(runtime / receipt_name)
            sync_directory(runtime)
            if activation:
                await call(activation["commit"])
            write_atomic(journal, "committed", b"1")
            await recover(runtime, receipt_name, is_activated)
            return next_receipt
        except BaseException as error:
            try:
                await recover(runtime, receipt_name, is_activated)
            except BaseException as restore_error:
                raise BaseExceptionGroup(
                    "Skill installation failed and recovery remains pending", [error, restore_error]
                ) from None
            raise
