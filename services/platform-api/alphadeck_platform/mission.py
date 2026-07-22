"""Mission contract compiler, ported from src/lib/agent/mission/compiler.ts."""

import hashlib
import re
from copy import deepcopy
from datetime import UTC, datetime


def identifier(value: str, label: str, maximum: int = 256) -> str:
    normalized = value.strip()
    if not normalized or len(normalized.encode()) > maximum:
        raise ValueError(f"{label} must be between 1 and {maximum} UTF-8 bytes.")
    return normalized


def safe_artifact(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if (
        not normalized
        or normalized.startswith("/")
        or "\x00" in normalized
        or ".." in normalized.split("/")
        or len(normalized.encode()) > 1024
    ):
        raise ValueError(f"Invalid MissionSpec artifact path: {value}")
    return normalized


def safe_integer(value, minimum: int) -> bool:
    return type(value) is int and minimum <= value <= 9007199254740991


def compile_nodes(nodes: list[dict]) -> list[dict]:
    compiled = {}
    for raw in nodes:
        node = deepcopy(raw)
        key = node["key"]
        if key in compiled:
            raise ValueError(f"Duplicate Mission node: {key}")
        budget = node["budget"]
        for name, minimum in (
            ("maxAttempts", 1),
            ("maxToolCalls", 0),
            ("maxInputTokens", 0),
            ("maxOutputTokens", 0),
            ("timeoutMs", 1),
        ):
            if not safe_integer(budget[name], minimum):
                raise ValueError(f"Mission node {key} contains an invalid budget.")
        for name in ("inputArtifacts", "outputArtifacts"):
            node[name] = [safe_artifact(value) for value in node[name]]
        compiled[key] = node
    for node in compiled.values():
        for dependency in node["dependencies"]:
            if dependency not in compiled:
                raise ValueError(
                    f"Mission node {node['key']} references missing dependency {dependency}."
                )
            if dependency == node["key"]:
                raise ValueError(f"Mission node {node['key']} cannot depend on itself.")
    return list(compiled.values())


def compile_mission(data: dict) -> dict:
    if not safe_integer(data["maxRepairAttempts"], 0):
        raise ValueError("maxRepairAttempts must be a non-negative safe integer.")
    objective = data["objective"].strip()
    if not objective:
        raise ValueError("Mission objective cannot be empty.")
    definition = data["definition"]
    identifier(definition["id"], "missionDefinition.id")
    identifier(definition["version"], "missionDefinition.version", 64)
    required = list(
        dict.fromkeys(
            identifier(value, "validation check ID", 128)
            for value in definition["requiredValidationCheckIds"]
        )
    )
    warnings = list(
        dict.fromkeys(
            identifier(value, "validation warning ID", 128)
            for value in definition["allowedValidationWarnings"]
        )
    )
    if set(warnings) - set(required):
        raise ValueError("Allowed validation warnings must reference required validation checks.")
    predicates = []
    predicate_ids = set()
    for raw in definition["acceptancePredicates"]:
        predicate = deepcopy(raw)
        predicate["id"] = identifier(predicate["id"], "acceptance predicate ID", 128)
        if predicate["id"] in predicate_ids:
            raise ValueError(f"Duplicate acceptance predicate: {predicate['id']}")
        predicate_ids.add(predicate["id"])
        predicates.append(predicate)
    entities = {}
    for entity in data.get("entities", []):
        kind = identifier(entity["entityType"], "entityType", 128)
        canonical_id = identifier(entity["canonicalId"], "canonicalId")
        entities[kind, canonical_id] = {"entityType": kind, "canonicalId": canonical_id}
    composition = deepcopy(data["composition"])
    for name in ("profileId", "profileVersion", "deliveryPackId", "deliveryPackVersion"):
        maximum = 64 if name.endswith("Version") else 256
        composition[name] = identifier(composition[name], name, maximum)
    composition_hash = composition["compositionSha256"].strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", composition_hash):
        raise ValueError("compositionSha256 must be a SHA-256 identity.")
    composition["compositionSha256"] = composition_hash
    packs = {}
    for pack in composition["domainPacks"]:
        key = identifier(pack["id"], "domainPackId")
        packs[key] = {"id": key, "version": identifier(pack["version"], "domainPackVersion", 64)}
    composition["domainPacks"] = sorted(packs.values(), key=lambda item: item["id"])
    artifacts = {}
    for raw in definition["artifacts"]:
        artifact = deepcopy(raw)
        artifact["path"] = safe_artifact(artifact["path"])
        existing = artifacts.get(artifact["path"])
        if existing is not None and existing != artifact:
            raise ValueError(f"Conflicting MissionSpec artifact contract: {artifact['path']}")
        artifacts[artifact["path"]] = artifact
    return {
        "schemaVersion": 1,
        "framework": "PI Agent",
        "projectId": identifier(data["projectId"], "projectId"),
        "requestId": identifier(data["requestId"], "requestId"),
        "objectiveSha256": "sha256:" + hashlib.sha256(objective.encode()).hexdigest(),
        "composition": composition,
        "capabilityId": identifier(data["capabilityId"], "capabilityId"),
        "runPlanId": identifier(data["runPlanId"], "runPlanId"),
        "validationReportPath": safe_artifact(definition["validationReportPath"]),
        "expectedEntities": [entities[key] for key in sorted(entities)],
        "artifacts": sorted(artifacts.values(), key=lambda item: item["path"]),
        "requiredValidationCheckIds": required,
        "allowedValidationWarnings": warnings,
        "maxRepairAttempts": data["maxRepairAttempts"],
        "nodes": compile_nodes(definition["nodes"]),
        "acceptancePredicates": predicates,
        "createdAt": data.get(
            "createdAt", datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        ),
    }
