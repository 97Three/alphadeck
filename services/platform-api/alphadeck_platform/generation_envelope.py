import hashlib
import re
from copy import deepcopy

from .finance_catalog import FinanceCatalog
from .integration_config import integration_scope
from .messages import js_length
from .provenance import js_json


def record(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object.")
    return value


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{1,127}", value):
        raise ValueError(label + " must be a stable lowercase identifier.")
    return value


def scope_identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", value):
        raise ValueError(label + " must be a bounded scope identifier.")
    return value


def version(value, label):
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9a-z.-]+)?", value, re.I
    ):
        raise ValueError(label + " must be a semantic version.")
    return value


def sha256(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError(label + " must be a SHA-256 identity.")
    return value


def parse_composition(value):
    value = record(value, "composition")
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise ValueError("Unsupported Data Agent composition lock schema.")
    profile, delivery, capability = [
        record(value.get(key), "composition." + key)
        for key in ("profile", "deliveryPack", "capability")
    ]
    if not isinstance(value.get("domainPacks"), list) or not value["domainPacks"]:
        raise ValueError("composition.domainPacks must contain at least one Domain Pack.")
    domains = []
    for index, pack in enumerate(value["domainPacks"]):
        label = f"composition.domainPacks[{index}]"
        pack = record(pack, label)
        domains.append(
            {
                "id": identifier(pack.get("id"), label + ".id"),
                "version": version(pack.get("version"), label + ".version"),
            }
        )
    if len({pack["id"] for pack in domains}) != len(domains):
        raise ValueError("composition.domainPacks contains duplicate IDs.")
    unsigned = {
        "schemaVersion": 1,
        "profile": {
            "id": identifier(profile.get("id"), "composition.profile.id"),
            "version": version(profile.get("version"), "composition.profile.version"),
        },
        "domainPacks": domains,
        "deliveryPack": {
            "id": identifier(delivery.get("id"), "composition.deliveryPack.id"),
            "version": version(delivery.get("version"), "composition.deliveryPack.version"),
        },
        "capability": {"id": identifier(capability.get("id"), "composition.capability.id")},
    }
    digest = sha256(value.get("sha256"), "composition.sha256")
    if digest != "sha256:" + hashlib.sha256(js_json(unsigned).encode()).hexdigest():
        raise ValueError("composition.sha256 does not match the composition contents.")
    return {**unsigned, "sha256": digest}


def parse_scope(value):
    value = record(value, "scope")
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise ValueError("Unsupported Data Agent scope schema.")
    if not isinstance(value.get("domainPackIds"), list) or not value["domainPackIds"]:
        raise ValueError("scope.domainPackIds must contain at least one Domain Pack.")
    domains = [
        identifier(identity, f"scope.domainPackIds[{index}]")
        for index, identity in enumerate(value["domainPackIds"])
    ]
    if len(set(domains)) != len(domains):
        raise ValueError("scope.domainPackIds contains duplicate IDs.")
    return {
        "schemaVersion": 1,
        **{
            key: scope_identifier(value.get(key), "scope." + key)
            for key in ("consumerId", "tenantId", "projectId", "workspaceId", "requestId")
        },
        "agentProfileId": identifier(value.get("agentProfileId"), "scope.agentProfileId"),
        "domainPackIds": domains,
        "integrationScopeSha256": sha256(
            value.get("integrationScopeSha256"), "scope.integrationScopeSha256"
        ),
    }


def parse_generation_envelope(value):
    if not isinstance(value, dict):
        raise ValueError("Data Agent generation envelope must be an object.")
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 3:
        raise ValueError("Unsupported Data Agent generation envelope schema.")
    if value.get("kind") != "data-agent.generation":
        raise ValueError("Unsupported Data Agent generation envelope kind.")
    if "payload" not in value:
        raise ValueError("Data Agent generation envelope payload is required.")
    return {
        "schemaVersion": 3,
        "kind": "data-agent.generation",
        "composition": parse_composition(value.get("composition")),
        "scope": parse_scope(value.get("scope")),
        "payload": value["payload"],
    }


def create_finance_envelope(payload, project, request, capability, *, scope=None, catalog=None):
    application = (catalog or FinanceCatalog()).resolve(capability_id=capability)
    scope = scope if scope is not None else integration_scope(project)
    return {
        "schemaVersion": 3,
        "kind": "data-agent.generation",
        "composition": application["composition"],
        "scope": {
            "schemaVersion": 1,
            "consumerId": scope["consumerId"],
            "tenantId": scope["memory"]["tenantId"],
            "projectId": project,
            "workspaceId": project,
            "requestId": request,
            "agentProfileId": application["profile"]["id"],
            "domainPackIds": [pack["id"] for pack in application["domainPacks"]],
            "integrationScopeSha256": scope["scopeSha256"],
        },
        "payload": deepcopy(payload),
    }


def assert_registered_finance(envelope, catalog=None):
    composition, scope = envelope["composition"], envelope["scope"]
    application = (catalog or FinanceCatalog()).resolve(
        composition["profile"]["id"], composition["capability"]["id"]
    )
    if (
        application["composition"] != composition
        or scope["agentProfileId"] != application["profile"]["id"]
        or scope["domainPackIds"] != [pack["id"] for pack in application["domainPacks"]]
    ):
        raise ValueError("Finance generation composition does not match the registered profile.")


def assert_finance_composition(envelope, plan, mission_spec):
    composition, scope = envelope["composition"], envelope["scope"]
    if (
        plan["composition"]["sha256"] != composition["sha256"]
        or (plan.get("requestedCapabilityId") or plan["capabilityId"])
        != composition["capability"]["id"]
    ):
        raise ValueError("Finance run plan composition does not match the dispatch envelope.")
    expected = {
        "profileId": composition["profile"]["id"],
        "profileVersion": composition["profile"]["version"],
        "domainPacks": composition["domainPacks"],
        "deliveryPackId": composition["deliveryPack"]["id"],
        "deliveryPackVersion": composition["deliveryPack"]["version"],
        "compositionSha256": composition["sha256"],
    }
    if (
        mission_spec["projectId"] != scope["projectId"]
        or mission_spec["requestId"] != scope["requestId"]
        or mission_spec["runPlanId"] != plan["runId"]
        or mission_spec["capabilityId"] != composition["capability"]["id"]
        or any(mission_spec["composition"].get(key) != item for key, item in expected.items())
    ):
        raise ValueError("PI Agent Mission composition does not match the dispatch envelope.")


def required_text(value, label, maximum=100000):
    if not isinstance(value, str) or not value.strip() or js_length(value) > maximum:
        raise ValueError(label + " must be a non-empty bounded string.")
    return value


def nullable_text(value, label):
    return None if value is None else required_text(value, label, 512)


def parse_finance_payload(envelope):
    assert_registered_finance(envelope)
    payload = record(envelope["payload"], "finance generation payload")
    if payload.get("cliPreference") != "pi":
        raise ValueError("Finance generation only supports the PI Agent runtime.")
    for key in ("isInitialPrompt", "usePrefetchedSelectionDashboard"):
        if type(payload.get(key)) is not bool:
            raise ValueError(key + " must be a boolean.")
    result = {
        key: required_text(
            payload.get(key),
            key,
            100000 if key in {"effectiveInstruction", "userVisibleInstructionForRepair"} else 512,
        )
        for key in (
            "effectiveInstruction",
            "userVisibleInstructionForRepair",
            "selectedModel",
            "memorySubjectId",
            "missionId",
            "generationId",
            "governedKnowledgeTaskCategory",
        )
    }
    result.update(
        {
            "cliPreference": "pi",
            **{key: payload[key] for key in ("isInitialPrompt", "usePrefetchedSelectionDashboard")},
            **{
                key: nullable_text(payload.get(key), key)
                for key in ("conversationId", "actorUserId")
            },
        }
    )
    images = payload.get("processedImages")
    if not isinstance(images, list) or len(images) > 8:
        raise ValueError("processedImages must be an array with at most eight entries.")
    result["processedImages"] = []
    for index, raw in enumerate(images):
        image = record(raw, f"processedImages[{index}]")
        size = image.get("size")
        # Match Number(value), including legacy string sizes, while retaining safe integer bounds.
        try:
            size = (
                0
                if size is None or size is False or size == ""
                else 1
                if size is True
                else float(size)
            )
            if size != int(size) or not 0 <= size <= 9007199254740991:
                raise ValueError
        except (ValueError, TypeError, OverflowError) as cause:
            raise ValueError(
                f"processedImages[{index}].size must be a non-negative integer."
            ) from cause
        result["processedImages"].append(
            {
                **{
                    key: required_text(image.get(key), f"processedImages[{index}].{key}", maximum)
                    for key, maximum in (
                        ("name", 512),
                        ("path", 4096),
                        ("url", 4096),
                        ("publicUrl", 4096),
                        ("mimeType", 128),
                    )
                },
                "size": int(size),
            }
        )
    for key, states, count_key in (
        (
            "personalizationRecall",
            {"disabled", "opted_out", "unavailable", "empty", "prepared"},
            "exposedMemoryCount",
        ),
        (
            "governedKnowledgePreparation",
            {"disabled", "unavailable", "empty", "prepared"},
            "passageCount",
        ),
    ):
        item = record(payload.get(key), key)
        if item.get("status") not in states:
            raise ValueError(key + ".status is invalid.")
        for count_field in (
            [count_key] if key == "personalizationRecall" else [count_key, "citationCount"]
        ):
            if (
                type(item.get(count_field)) is not int
                or not 0 <= item[count_field] <= 9007199254740991
            ):
                raise ValueError(key + "." + count_field + " must be a non-negative integer.")
        if item.get("capsule") is not None:
            record(item.get("capsule"), key + ".capsule")
        if key == "personalizationRecall" and item.get("preparedUse") is not None:
            record(item.get("preparedUse"), key + ".preparedUse")
        if item["status"] == "prepared" and (
            item.get("capsule") is None
            or item[count_key] == 0
            or (
                item.get("preparedUse") is None
                if key == "personalizationRecall"
                else item["citationCount"] == 0
            )
        ):
            raise ValueError(
                "Prepared personalization recall requires a capsule, prepared use, and exposed memory."
                if key == "personalizationRecall"
                else "Prepared governed knowledge requires a capsule and citations."
            )
        result[key] = deepcopy(item)
    return result


class GenerationRuntimeRegistry:
    def __init__(self):
        self.handlers = {}

    def register(self, profile, handler):
        profile = identifier(profile, "profileId")
        if profile in self.handlers:
            raise ValueError("Duplicate Data Agent generation handler: " + profile)
        self.handlers[profile] = handler
        return self

    async def execute(self, job, *, fence=None):
        envelope = parse_generation_envelope(job["executionEnvelope"])
        profile = envelope["composition"]["profile"]["id"]
        if profile not in self.handlers:
            raise ValueError("No generation handler is registered for " + profile + ".")
        return await self.handlers[profile]({**job, "executionEnvelope": envelope}, fence=fence)
