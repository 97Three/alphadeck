import re

from .finance_catalog import FinanceCatalog
from .finance_tools import FinanceTools
from .skill_catalog import TARGETS, SkillCatalogue
from .skill_compiler import metadata
from .skill_dashboard import dashboard
from .skill_install import read_receipt
from .skill_integrity import hash_directory, read_regular


def compatibility(capsule, phase, available):
    missing = [name for name in capsule["requiresTools"] if name not in available]
    alternatives = capsule.get("requiresOneOfToolSets") or []
    missing_alternative = bool(
        alternatives and not any(all(name in available for name in group) for group in alternatives)
    )
    allowed = phase in capsule["phases"]
    return {
        "compatible": allowed and not missing and not missing_alternative,
        "phaseAllowed": allowed,
        "missingTools": missing,
        "missingAlternative": missing_alternative,
    }


def installation(workspace, skills, target="pi-agent", trusted=None):
    try:
        receipt = read_receipt(workspace, target)
    except (ValueError, OSError):
        return {
            "receiptStatus": "invalid",
            "installedAt": None,
            "skills": [
                {
                    "skillId": skill["id"],
                    "state": "unverified",
                    "installedVersion": None,
                    "expectedVersion": skill["version"],
                }
                for skill in skills
            ],
        }
    directory = workspace / TARGETS[target][0] / "skills"
    exists = directory.exists() or directory.is_symlink()
    safe = directory.is_dir() and not directory.is_symlink()
    claims = (receipt or {}).get("skills", {})
    statuses = []
    for skill in skills:
        identity = skill["id"]
        result = {
            "skillId": identity,
            "state": "missing",
            "installedVersion": None,
            "expectedVersion": skill["version"],
        }
        if exists and not safe:
            result["state"] = "unverified"
        elif safe:
            try:
                if not (directory / identity).exists() and not (directory / identity).is_symlink():
                    raise FileNotFoundError(identity)
                actual = hash_directory(directory / identity)
                claim = claims.get(identity)
                if (
                    not claim
                    or not isinstance(claim.get("version"), str)
                    or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", claim["version"])
                    or not isinstance(claim.get("sourceSha256"), str)
                ):
                    result["state"] = "unverified"
                elif (
                    trusted
                    and (
                        identity not in trusted["skillIds"]
                        or trusted["lock"]["skills"].get(identity, {}).get("version")
                        != claim["version"]
                        or trusted["lock"]["skills"].get(identity, {}).get("sourceSha256")
                        != actual["hash"]
                        or trusted["lock"]["skills"].get(identity, {}).get("packageSha256")
                        != claim.get("packageSha256")
                    )
                    or actual["hash"] != claim["sourceSha256"]
                ):
                    result["state"] = "modified"
                else:
                    result["installedVersion"] = claim["version"]
                    result["state"] = (
                        "outdated"
                        if claim["version"] != skill["version"]
                        else "modified"
                        if actual["hash"] != skill["lock"]["sourceSha256"]
                        or claim.get("packageSha256") != skill["lock"]["packageSha256"]
                        else "current"
                    )
            except FileNotFoundError:
                result["state"] = "missing"
            except (ValueError, OSError):
                result["state"] = "unverified"
        statuses.append(result)
    return {
        "receiptStatus": "present" if receipt else "missing",
        "installedAt": (receipt or {}).get("installedAt"),
        "skills": statuses,
    }


def market(catalogue_root, project=None):
    catalogue = SkillCatalogue(catalogue_root)
    state = catalogue.read()
    root = catalogue.resolve(state["active"]) if state["active"] else catalogue.root
    data = dashboard(root)
    target = project.get("target", "pi-agent") if project else "pi-agent"
    deployment = (
        state["deployments"].get(catalogue.deployment_key(project["workspace"], target))
        if project
        else None
    )
    pinned = metadata(catalogue.resolve(deployment["revision"]))[1] if deployment else None
    capsules = metadata(root)[2]["skills"]
    tools = FinanceTools(root, include_dashboard_spec=True).registered
    capabilities = FinanceCatalog().data["capabilities"]
    skills = []
    for skill in data["skills"]:
        identity, capsule = skill["id"], capsules[skill["id"]]
        skills.append(
            {
                **{
                    key: skill[key]
                    for key in (
                        "id",
                        "name",
                        "scope",
                        "version",
                        "status",
                        "boundary",
                        "inputs",
                        "outputs",
                        "validation",
                    )
                },
                "health": skill["health"]["status"],
                "integrityProblems": skill["health"]["missing"],
                "packageVerified": skill["health"]["status"] == "ok"
                and skill["package"]["exists"]
                and skill["status"] == "stable",
                "packageSha256": skill["lock"]["packageSha256"],
                "releases": [
                    {
                        **{
                            key: release[key]
                            for key in ("actor", "version", "date", "summary", "changes")
                            if key in release
                        },
                        "installable": any(
                            item["skillId"] == identity and item["version"] == release["version"]
                            for item in state["releases"]
                        )
                        or not state["active"]
                        and release["version"] == skill["version"],
                    }
                    for release in skill["changelog"]["releases"]
                ],
                "capabilities": [
                    capability["id"]
                    for capability in capabilities
                    if identity in capability["requiredSkills"]
                ],
                "phases": [
                    {
                        "phase": phase,
                        **compatibility(capsule, phase, set() if phase == "platform-ui" else tools),
                    }
                    for phase in capsule["phases"]
                ],
                "requiredTools": capsule["requiresTools"],
                "toolAlternatives": capsule.get("requiresOneOfToolSets", []),
                "activation": "platform-only" if skill["scope"] == "platform" else "task-selected",
            }
        )
    view = None
    if project:
        view = {
            "id": project["id"],
            "target": target,
            "deployment": {
                **{
                    key: deployment[key]
                    for key in ("id", "revision", "skillIds", "updatedAt", "actor")
                },
                "canRollback": bool(deployment.get("previous")),
            }
            if deployment
            else None,
            "execution": "pinned"
            if target == "pi-agent" and deployment
            else "platform-current"
            if target == "pi-agent"
            else "external-unverified",
            **installation(
                project["workspace"],
                data["skills"],
                target,
                {"skillIds": deployment["skillIds"], "lock": pinned} if deployment else None,
            ),
        }
    return {
        "generatedAt": data["generatedAt"],
        "targets": [
            {
                "id": key,
                "label": label,
                "directory": directory,
                "receipt": receipt,
                "runtimeVerified": key == "pi-agent",
            }
            for key, (directory, receipt, label) in TARGETS.items()
        ],
        "skills": skills,
        "capabilities": [
            {key: capability[key] for key in ("id", "name", "status")}
            for capability in capabilities
        ],
        "project": view,
    }


def verified_package(catalogue_root, identity):
    catalogue = SkillCatalogue(catalogue_root)
    root, _ = catalogue.runtime()
    skill = next((item for item in dashboard(root)["skills"] if item["id"] == identity), None)
    if (
        skill is None
        or skill["health"]["status"] != "ok"
        or not skill["package"]["exists"]
        or skill["status"] != "stable"
    ):
        raise ValueError("技能未发布或完整性校验失败，暂不可下载。")
    from .skill_catalog import checksum

    content = read_regular(root / skill["package"]["path"], 5242880)
    digest = checksum(content)
    if digest != skill["lock"]["packageSha256"]:
        raise ValueError("技能包已变更，请刷新后重试。")
    return {"content": content, "sha256": "sha256:" + digest, "version": skill["version"]}
