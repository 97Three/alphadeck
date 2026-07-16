import hashlib
import json
from copy import deepcopy

from .config import ROOT


class FinanceCatalog:
    def __init__(self, root=ROOT):
        self.data = json.loads((root / "config/finance-catalog.json").read_text(encoding="utf-8"))
        if self.data.get("schemaVersion") != 1:
            raise ValueError("Unsupported finance catalogue schema")
        self.by_id = {item["id"]: item for item in self.data["capabilities"]}

    def capability(self, identity=None):
        identity = identity or self.data["profile"]["defaultCapabilityId"]
        return deepcopy(
            self.by_id.get(identity, self.by_id[self.data["profile"]["defaultCapabilityId"]])
        )

    def resolve(self, profile_id=None, capability_id=None, output=None):
        profile = self.data["profile"]
        if profile_id is not None and profile_id != profile["id"]:
            raise ValueError(f"Unknown Data Agent profile: {profile_id}")
        selected = capability_id.strip() if capability_id else profile["defaultCapabilityId"]
        matches = [
            item for item in self.data["domainPack"]["capabilities"] if item["id"] == selected
        ]
        if len(matches) != 1:
            raise ValueError(f"Unknown or unavailable capability: {selected}")
        if matches[0]["status"] != "ready":
            raise ValueError(
                f"Capability {selected} is not ready for execution (status={matches[0]['status']})."
            )
        delivery = self.data["deliveryPack"]
        supported = set(delivery["supportedOutputs"]) & set(matches[0]["supportedOutputs"])
        if not supported or (output is not None and output not in supported):
            raise ValueError("Output is incompatible with capability and delivery pack")
        unsigned = {
            "schemaVersion": 1,
            "profile": {"id": profile["id"], "version": profile["version"]},
            "domainPacks": [
                {"id": self.data["domainPack"]["id"], "version": self.data["domainPack"]["version"]}
            ],
            "deliveryPack": {"id": delivery["id"], "version": delivery["version"]},
            "capability": {"id": selected},
        }
        encoded = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":")).encode()
        return deepcopy(
            {
                "profile": profile,
                "deliveryPack": delivery,
                "domainPacks": [self.data["domainPack"]],
                "capability": matches[0],
                "composition": {
                    **unsigned,
                    "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
                },
            }
        )

    def quant_settings(self, capability_id=None):
        capability = self.capability(capability_id)
        execution = (
            capability
            if capability["status"] == "ready"
            else self.capability(capability["executionCapabilityId"])
        )
        return {
            "capabilityId": capability["id"],
            "agentType": capability["agentType"],
            "subAgentKey": capability["subAgentKey"],
            "executionCapabilityId": execution["id"],
            "status": capability["status"],
            "requiredSkills": capability["requiredSkills"],
            "dataEndpoints": execution["dataEndpoints"],
            "expectedArtifacts": execution["expectedArtifacts"],
            "validationRules": capability["validationRules"],
        }

    def serialized_capabilities(self):
        result = []
        for capability in self.data["capabilities"]:
            settings = self.quant_settings(capability["id"])
            fields = (
                "id",
                "name",
                "shortName",
                "description",
                "inputHint",
                "tags",
                "status",
                "groupId",
                "agentType",
                "subAgentKey",
                "executionCapabilityId",
                "requiredSkills",
                "validationRules",
            )
            result.append(
                {
                    **{key: capability[key] for key in fields},
                    "dataEndpoints": settings["dataEndpoints"],
                    "expectedArtifacts": settings["expectedArtifacts"],
                }
            )
        return result
