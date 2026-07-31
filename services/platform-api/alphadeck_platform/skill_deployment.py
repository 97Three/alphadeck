import json
import shutil
from pathlib import Path
from uuid import uuid4

from .config import ROOT
from .skill_catalog import SkillCatalogue, now, valid_skill
from .skill_compiler import compile_skills, metadata
from .skill_install import TARGETS, install_assets, read_receipt
from .skill_integrity import hash_directory
from .workspace import write_atomic


class SkillConflict(Exception):
    status = 409


def replace_skill(target, source, identity):
    target_registry, target_lock, target_capsules = metadata(target)
    source_registry, source_lock, source_capsules = metadata(source)
    definition = next(
        (entry for entry in source_registry["coreSkills"] if entry["id"] == identity), None
    )
    if not definition or not any(
        entry["id"] == identity for entry in target_registry["coreSkills"]
    ):
        raise ValueError("Skill is not registered")
    target_registry["coreSkills"] = [
        definition if entry["id"] == identity else entry for entry in target_registry["coreSkills"]
    ]
    target_lock["skills"][identity] = source_lock["skills"][identity]
    target_capsules["skills"][identity] = source_capsules["skills"][identity]
    destination = target / ".pi/skills" / identity
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source / ".pi/skills" / identity, destination)
    shutil.copy2(
        source / ".pi/skill-packages" / (identity + ".tgz"),
        target / ".pi/skill-packages" / (identity + ".tgz"),
    )
    for relative, value in (
        (".pi/skills.registry.json", target_registry),
        (".pi/skills.lock.json", target_lock),
        ("config/pi-agent-skill-capsules.json", target_capsules),
    ):
        write_atomic(
            target, relative, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        )


async def deploy_skills(
    workspace,
    *,
    target="pi-agent",
    action="initialize",
    skill_id=None,
    version=None,
    skill_ids=None,
    expected_revision=None,
    actor="project-initialization",
    overwrite_modified=False,
    root=ROOT,
    binding_workspace=None,
    fresh_project=False,
    deployment_id=None,
):
    if target not in TARGETS or (skill_id is not None and not valid_skill(skill_id)):
        raise ValueError("Invalid Skill deployment request")
    if action not in {"initialize", "install", "uninstall", "rollback"}:
        raise ValueError("Unknown Skill deployment action")
    workspace = Path(workspace).resolve(strict=True)
    if fresh_project and (
        action != "initialize"
        or binding_workspace is None
        or Path(binding_workspace).exists()
        or Path(binding_workspace).is_symlink()
    ):
        raise ValueError(
            "Fresh project Skill initialization requires an unpublished managed workspace"
        )
    catalog = SkillCatalogue(root)
    key = catalog.deployment_key(binding_workspace or workspace, target)
    async with catalog.lock():
        state = catalog.read()
        previous = state["deployments"].get(key)
        if action == "initialize" and previous and not fresh_project:
            return previous
        if not fresh_project and (previous["id"] if previous else None) != expected_revision:
            raise SkillConflict("Project Skill revision changed; refresh before retrying")
        catalog.initialize(state)
        active_root = catalog.resolve(state["active"], verify_all=True)
        active_registry, active_lock, _ = metadata(active_root)
        selected = (
            list(previous["skillIds"])
            if previous
            else [
                entry["id"]
                for entry in active_registry["coreSkills"]
                if target == "pi-agent" and entry["status"] == "stable"
            ]
        )
        revision = previous["revision"] if previous and not fresh_project else state["active"]
        image = catalog.resolve(revision, verify_all=True)
        trusted = {}
        if previous and not fresh_project:
            _, previous_lock, _ = metadata(image)
            trusted = {
                identity: {
                    "version": previous_lock["skills"][identity]["version"],
                    "source": "source",
                    "sourceSha256": previous_lock["skills"][identity].get("sourceSha256"),
                    "packageSha256": previous_lock["skills"][identity].get("packageSha256"),
                }
                for identity in previous["skillIds"]
            }
        else:
            receipt = read_receipt(workspace, target)
            for identity, claim in (receipt or {}).get("skills", {}).items():
                locked = active_lock["skills"].get(identity)
                directory = workspace / TARGETS[target][0] / "skills" / identity
                if (
                    locked
                    and all(
                        locked.get(field) == claim.get(field)
                        for field in ("version", "sourceSha256", "packageSha256")
                    )
                    and directory.exists()
                    and hash_directory(directory)["hash"] == locked["sourceSha256"]
                ):
                    trusted[identity] = claim
        if action == "rollback":
            if not previous or not previous.get("previous"):
                raise ValueError("No previous Skill deployment exists")
            revision = previous["previous"]["revision"]
            selected = previous["previous"]["skillIds"]
            image = catalog.resolve(revision, verify_all=True)
        elif action == "initialize":
            if skill_ids is not None:
                selected = list(skill_ids)
        else:
            definition = next(
                (entry for entry in active_registry["coreSkills"] if entry["id"] == skill_id), None
            )
            if not definition:
                raise ValueError("Skill is not registered")
            if action == "uninstall":
                if not previous or skill_id not in selected:
                    raise ValueError("Skill is not installed")
                selected.remove(skill_id)
            else:
                requested = version or definition["version"]
                release = next(
                    (
                        item
                        for item in state["releases"]
                        if item["skillId"] == skill_id and item["version"] == requested
                    ),
                    None,
                )
                if not release:
                    raise ValueError("Skill version lacks a complete snapshot")
                stage = catalog.stage(image)
                try:
                    replace_skill(
                        stage, catalog.resolve(release["revision"], verify_all=True), skill_id
                    )
                    revision = catalog.seal(stage)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
                image = catalog.resolve(revision, verify_all=True)
                selected = list(dict.fromkeys([*selected, skill_id]))
        registry, lock, capsules = metadata(image)
        by_id = {entry["id"]: entry for entry in registry["coreSkills"]}
        for identity in selected:
            if not valid_skill(identity) or by_id.get(identity, {}).get("status") != "stable":
                raise ValueError("Only registered stable Skills may be installed")
            compile_skills(
                image,
                required_skill_ids=[identity],
                phase=capsules["skills"][identity]["phases"][0],
                budget=32000,
            )
        deployment = {
            "id": deployment_id or str(uuid4()),
            "revision": revision,
            "skillIds": sorted(selected),
            "target": target,
            "updatedAt": now(),
            "actor": actor,
        }
        if previous:
            deployment["previous"] = {
                "revision": previous["revision"],
                "skillIds": previous["skillIds"],
            }
        state["deployments"][key] = deployment
        catalog.event(
            state, "deployment." + action, actor, target=target, skillId=skill_id, version=version
        )
        assets = [
            {
                "id": identity,
                "claim": {
                    "version": lock["skills"][identity]["version"],
                    "source": "source",
                    "sourceSha256": lock["skills"][identity].get("sourceSha256"),
                    "packageSha256": lock["skills"][identity].get("packageSha256"),
                },
                "prepare": lambda destination, identity=identity: shutil.copytree(
                    image / ".pi/skills" / identity, destination
                ),
            }
            for identity in selected
        ]
        await install_assets(
            workspace,
            target,
            assets,
            trusted_managed=trusted,
            overwrite_modified=overwrite_modified,
            activation={
                "id": deployment["id"],
                "commit": lambda: catalog.commit(state),
                "isCommitted": lambda identity: (
                    catalog.read()["deployments"].get(key, {}).get("id") == identity
                ),
            },
        )
        return deployment
