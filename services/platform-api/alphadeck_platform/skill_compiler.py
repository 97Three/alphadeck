"""Compile governed Skills into the same bounded runtime capsules as the original engine."""

import hashlib
import io
import json
import re
import tarfile

from .config import ROOT
from .skill_integrity import hash_directory, read_regular

PHASES = {
    "planning",
    "data-preparation",
    "workspace-generation",
    "validation-repair",
    "platform-ui",
}
FORBIDDEN = re.compile(
    r"mcp__|\.pi/skills/|\bcurl\b|\bbash\b|\bpython3?\b|\bnpm\s+run\b|\bcat\s*>|\bheredoc\b", re.I
)


def characters(text):
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def digest(content):
    return hashlib.sha256(content if isinstance(content, bytes) else content.encode()).hexdigest()


def inside(root, relative):
    path = (root / relative).resolve()
    if path == root or root not in path.parents:
        raise ValueError("Skill path must be inside its catalogue")
    return path


def metadata(root):
    registry = json.loads(read_regular(root / ".pi/skills.registry.json", 2 * 1024 * 1024))
    lock = json.loads(read_regular(root / ".pi/skills.lock.json", 2 * 1024 * 1024))
    capsules = json.loads(
        read_regular(root / "config/pi-agent-skill-capsules.json", 2 * 1024 * 1024)
    )
    if any(item.get("schemaVersion") != 1 for item in (registry, lock, capsules)):
        raise ValueError("Unsupported Skills metadata schema")
    entries = registry["coreSkills"]
    identities = [entry["id"] for entry in entries]
    if len(set(identities)) != len(identities) or set(identities) != capsules["skills"].keys():
        raise ValueError("Skill registry and runtime capsule identities differ")
    for entry in entries:
        identity = entry["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identity):
            raise ValueError("Invalid Skill ID")
        capsule = capsules["skills"][identity]
        if FORBIDDEN.search(json.dumps(capsule, ensure_ascii=False)):
            raise ValueError("Runtime capsule contains incompatible execution instructions")
        if (
            type(capsule["priority"]) is not int
            or capsule["priority"] < 1
            or not capsule["phases"]
            or set(capsule["phases"]) - PHASES
        ):
            raise ValueError("Invalid runtime capsule priority or phases")
        for key in ("requiresTools", "invariants", "workflow", "doneWhen"):
            if not isinstance(capsule[key], list) or any(
                not isinstance(value, str) or not value.strip() for value in capsule[key]
            ):
                raise ValueError(f"Invalid capsule {key}")
        if (
            not all(capsule[key] for key in ("invariants", "workflow", "doneWhen"))
            or not capsule["objective"].strip()
        ):
            raise ValueError("Runtime capsule steps must not be empty")
        tools = capsule["requiresTools"] + [
            tool for group in capsule.get("requiresOneOfToolSets", []) for tool in group
        ]
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", tool) for tool in tools):
            raise ValueError("Invalid capsule tool name")
        if any(not group for group in capsule.get("requiresOneOfToolSets", [])):
            raise ValueError("Empty alternative tool set")
        resource_ids = set()
        for resource in capsule["resources"]:
            if resource["id"] in resource_ids:
                raise ValueError("Duplicate resource ID")
            resource_ids.add(resource["id"])
            if (
                resource["selector"] not in {"named-headings", "template-heading"}
                or type(resource["maxChars"]) is not int
                or resource["maxChars"] < 256
                or type(resource["required"]) is not bool
                or not resource["profiles"]
                or set(resource["profiles"]) - set(capsule["phases"])
            ):
                raise ValueError("Invalid capsule resource")
            if resource["selector"] == "named-headings" and not resource.get("headings"):
                raise ValueError("Named resource requires headings")
    return registry, lock, capsules


def package_files(package, identity, expected):
    files, names, total = {}, set(), 0
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:gz") as archive:
        for member in archive:
            name = re.sub(r"^(?:\./)+", "", member.name).rstrip("/")
            if (
                not name
                or "\\" in name
                or name.startswith("/")
                or "\x00" in name
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or (name != identity and not name.startswith(identity + "/"))
                or name in names
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Unsafe Skill package entry")
            names.add(name)
            if len(names) > 500:
                raise ValueError("Skill package exceeds entry limit")
            if member.isfile():
                relative = name[len(identity) + 1 :]
                if not relative or not 0 <= member.size <= 10 * 1024 * 1024:
                    raise ValueError("Invalid Skill package file")
                total += member.size
                if total > 50 * 1024 * 1024 or (
                    relative.startswith("scripts/") and not member.mode & 0o111
                ):
                    raise ValueError("Skill package exceeds limits or has a non-executable script")
                files[relative] = archive.extractfile(member).read()
    required = {
        identity + suffix
        for suffix in ("/SKILL.md", "/agents/openai.yaml", "/references", "/scripts")
    }
    if not required <= names:
        raise ValueError("Incomplete Skill package")
    checksum = hashlib.sha256()
    for path in sorted(files):
        checksum.update(path.encode() + b"\0" + files[path] + b"\0")
    if checksum.hexdigest() != expected["sourceSha256"] or len(files) != expected["fileCount"]:
        raise ValueError("Skill package content differs from source lock")
    return files


def load_skill(root, entry, expected):
    if expected is None or expected["version"] != entry["version"]:
        raise ValueError("Skill version lock mismatch")
    identity = entry["id"]
    source = root / ".pi/skills" / identity
    package = root / expected.get("packagePath", f".pi/skill-packages/{identity}.tgz")
    inside(root, str(package))
    packed = None
    if package.exists() or package.is_symlink():
        packed = read_regular(package, 50 * 1024 * 1024)
        if digest(packed) != expected.get("packageSha256"):
            raise ValueError(f"Skill package hash mismatch: {identity}")
    if source.exists() or source.is_symlink():
        inside(root, str(source))
        actual = hash_directory(source)
        if actual["hash"] != expected.get("sourceSha256") or actual["fileCount"] != expected.get(
            "fileCount"
        ):
            raise ValueError(f"Skill source hash mismatch: {identity}")
        markdown = read_regular(source / "SKILL.md", 4 * 1024 * 1024).decode("utf-8")
        return {
            "registry": entry,
            "lock": expected,
            "source": "source",
            "directory": source,
            "files": None,
            "markdown": markdown,
        }
    if packed is None:
        raise ValueError(f"No verified Skill source or package: {identity}")
    files = package_files(packed, identity, expected)
    return {
        "registry": entry,
        "lock": expected,
        "source": "package",
        "directory": None,
        "files": files,
        "markdown": files["SKILL.md"].decode("utf-8"),
    }


def compile_resources(skill, capsule, phase, template_id=None):
    result = []
    for resource in capsule["resources"]:
        if phase not in resource["profiles"]:
            continue
        path = resource["path"].strip().replace("\\", "/").removeprefix("./")
        if (
            not path.startswith("references/")
            or ".." in path.split("/")
            or "\x00" in path
            or not path.endswith(".md")
        ):
            raise ValueError("Invalid Skill resource path")
        if skill["directory"]:
            content = read_regular(inside(skill["directory"], path), 4 * 1024 * 1024).decode(
                "utf-8"
            )
        else:
            content = skill["files"][path].decode("utf-8")
        headings = list(re.finditer(r"^##\s+(.+)$", content, re.M))
        sections = [
            (
                match[1].strip(),
                content[
                    match.start() : headings[index + 1].start()
                    if index + 1 < len(headings)
                    else len(content)
                ].strip(),
            )
            for index, match in enumerate(headings)
        ]
        if resource["selector"] == "template-heading":
            if not template_id or not template_id.strip():
                continue
            template_id = template_id.strip()
            selected = [
                (title, text)
                for title, text in sections
                if title == template_id or title.startswith(template_id + "：")
            ]
        else:
            selected = [(title, text) for title, text in sections if title in resource["headings"]]
            if resource["required"] and set(resource["headings"]) - {
                title for title, _ in selected
            }:
                raise ValueError("Required Skill resource headings are missing")
        text = "\n\n".join(text for _, text in selected)
        if resource["required"]:
            if not text or characters(text) > resource["maxChars"]:
                raise ValueError("Required resource is empty or exceeds its atomic budget")
        else:
            packed = []
            for _, section in selected:
                if characters("\n\n".join([*packed, section])) <= resource["maxChars"]:
                    packed.append(section)
            text = "\n\n".join(packed)
        if text:
            result.append(
                {
                    "id": resource["id"],
                    "path": resource["path"],
                    "text": text,
                    "sha256": digest(text),
                }
            )
    return result


def compile_skills(
    root=ROOT,
    *,
    phase="data-preparation",
    required_skill_ids=None,
    capability=None,
    capability_id=None,
    additional_skill_ids=(),
    activated_skill_ids=(),
    excluded_skill_ids=(),
    available_tools=None,
    template_id=None,
    variant_id=None,
    budget=6000,
    deployed_ids=None,
    runtime_workspace=None,
):
    from .skill_catalog import SkillCatalogue

    root = root.resolve()
    root, deployment = SkillCatalogue(root).runtime(runtime_workspace)
    if deployment:
        deployed_ids = (
            deployment["skillIds"]
            if deployed_ids is None
            else set(deployment["skillIds"]) & set(deployed_ids)
        )
    registry, lock, capsules = metadata(root)
    if capability and capability_id and capability["id"] != capability_id:
        raise ValueError("Skill capability identity mismatch")
    capability_id = capability["id"] if capability else capability_id
    if required_skill_ids is not None:
        requested = list(required_skill_ids)
    elif capability:
        requested = list(capability["requiredSkillIds"])
    elif capability_id:
        raise ValueError("A capability ID requires a domain-owned descriptor")
    else:
        requested = [entry["id"] for entry in registry["coreSkills"] if entry["status"] == "stable"]
    requested = list(
        dict.fromkeys(
            value for value in [*requested, *additional_skill_ids, *activated_skill_ids] if value
        )
    )
    if required_skill_ids is None:
        requested = [
            identity
            for identity in requested
            if identity not in excluded_skill_ids
            and phase in capsules["skills"].get(identity, {}).get("phases", [])
        ]
    if not requested or (deployed_ids is not None and set(requested) - set(deployed_ids)):
        raise ValueError("No selected Skills, or the pinned deployment lacks required Skills")
    if type(budget) is not int or budget < 256:
        raise ValueError("Skill context budget must be an integer of at least 256")
    by_id = {entry["id"]: entry for entry in registry["coreSkills"]}
    if (
        capability
        and capability["status"] == "ready"
        and any(
            by_id.get(identity, {}).get("status") == "planned"
            for identity in capability["requiredSkillIds"]
        )
    ):
        raise ValueError("Ready capabilities cannot depend on planned Skills")
    loaded = []
    for identity in requested:
        entry = by_id.get(identity)
        if not entry or entry["status"] == "deprecated":
            raise ValueError(f"Unknown or deprecated Skill: {identity}")
        capsule = capsules["skills"][identity]
        if phase not in capsule["phases"]:
            raise ValueError(f"Skill {identity} does not allow phase {phase}")
        if available_tools is not None:
            available = set(available_tools)
            alternatives = capsule.get("requiresOneOfToolSets", [])
            if set(capsule["requiresTools"]) - available or (
                alternatives and not any(set(group) <= available for group in alternatives)
            ):
                raise ValueError(f"Skill {identity} requires missing tools")
        loaded.append(load_skill(root, entry, lock["skills"].get(identity)))
    system = "\n".join(
        [
            "# PI Agent Skill Manifest",
            f"phase={phase}; capability={capability_id or 'explicit/default'}",
            "以下能力已经 registry/version/SHA-256 与 runtime capsule 校验；全局安全、权限和终止规则由 Kernel 负责，Skill 只补充领域步骤。",
            *[
                f"- {item['registry']['id']}@{item['registry']['version']} priority={capsules['skills'][item['registry']['id']]['priority']}: {item['registry']['boundary']}"
                for item in loaded
            ],
        ]
    )
    blocks, compiled = [], []
    for item in loaded:
        entry, expected = item["registry"], item["lock"]
        capsule = capsules["skills"][entry["id"]]
        resources = compile_resources(item, capsule, phase, template_id)
        lines = [
            f"## {entry['id']} — {entry['name']}",
            f"目标：{capsule['objective']}",
            *[f"必须：{text}" for text in capsule["invariants"]],
            *[f"步骤 {index + 1}：{text}" for index, text in enumerate(capsule["workflow"])],
            *[f"完成条件：{text}" for text in capsule["doneWhen"]],
        ]
        block = "\n\n".join(
            [
                "\n".join(lines),
                *[f"### {resource['id']}\n{resource['text']}" for resource in resources],
            ]
        )
        if FORBIDDEN.search(block) or characters(block) > budget:
            raise ValueError(
                "Atomic Skill capsule exceeds its budget or contains incompatible instructions"
            )
        blocks.append(block)
        compiled.append(
            {
                "id": entry["id"],
                "name": entry["name"],
                "version": entry["version"],
                "status": entry["status"],
                "source": item["source"],
                "sourceSha256": expected.get("sourceSha256"),
                "packageSha256": expected.get("packageSha256"),
                "originalCharacters": characters(item["markdown"]),
                "compiledCharacters": characters(block),
                "truncated": False,
                "capsuleSha256": digest(block),
                "includedResources": [
                    {key: resource[key] for key in ("id", "path", "sha256")}
                    | {"characters": characters(resource["text"])}
                    for resource in resources
                ],
            }
        )
    heading = (
        f"执行阶段：{phase}"
        + (f"；模板：{template_id}" if template_id else "")
        + (f"；变体：{variant_id}" if variant_id else "")
    )
    task = "\n\n".join(
        [
            "# PI Agent Skill Capsules",
            heading,
            "按以下原子步骤执行；不要从 SKILL.md 猜测未声明工具，也不要自行读取相对 reference 路径。",
            *blocks,
        ]
    )
    total = characters(system) + characters(task)
    if total > budget:
        raise ValueError(f"Atomic Skill context exceeds budget: {total} > {budget}")
    return {
        "runtime": "PI Agent",
        "capabilityId": capability_id,
        "selectedSkillIds": requested,
        "phase": phase,
        "systemContext": system,
        "taskContext": task,
        "maxSystemContextChars": budget,
        "systemContextCharacters": characters(system),
        "taskContextCharacters": characters(task),
        "totalCharacters": total,
        "truncated": False,
        "skills": compiled,
        "installReceipt": None,
    }
