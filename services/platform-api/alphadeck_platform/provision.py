import json

from .config import ROOT
from .finance_catalog import FinanceCatalog
from .llm_config import project_llm_config
from .skill_catalog import SkillCatalogue, now
from .skill_deployment import deploy_skills
from .workspace import workspace_file, write_atomic


class Provisioner:
    def __init__(self, root=ROOT):
        self.root = root
        self.catalog = FinanceCatalog(root)

    async def provision(self, stage, target, project, application, attempt):
        capability = self.catalog.capability(application["capability"]["id"])
        visual = self.catalog.data["applications"][capability["id"]]["visualization"]
        llm = project_llm_config(project["selectedModel"])
        timestamp = now()
        for relative in application["deliveryPack"]["workspaceDirectories"]:
            workspace_file(stage, relative).mkdir(parents=True, exist_ok=True)
        workspace = {
            "schemaVersion": 1,
            "workspaceId": project["projectId"],
            "projectId": project["projectId"],
            "projectName": project["name"],
            "platform": "AlphaDeck",
            "composition": application["composition"],
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "runtime": {
                "framework": "PI Agent",
                "executorId": "pi",
                "modelId": project["selectedModel"],
                "modelProfileId": llm["profileId"],
            },
        }
        selection = {
            "schemaVersion": 1,
            "profile": application["profile"],
            "selectedCapabilityId": capability["id"],
            "composition": application["composition"],
            "selectionSource": project.get("capabilitySelectionSource", "default"),
            "updatedAt": timestamp,
        }
        visualization = {
            "required": False,
            **{
                key: visual[key]
                for key in (
                    "templateId",
                    "name",
                    "scenario",
                    "variantId",
                    "variantName",
                    "variantScenario",
                    "layout",
                    "density",
                    "firstViewport",
                    "variantGuidance",
                    "matchReasons",
                    "painPoints",
                    "dataSignals",
                    "finalDataContract",
                )
            },
            "panels": visual["requiredComponents"],
            "optionalPanels": visual["optionalComponents"],
        }
        plan = {
            "schemaVersion": 1,
            "runId": None,
            "status": "pending",
            "capabilityId": capability["id"],
            "composition": application["composition"],
            "llm": llm,
            "question": "",
            "symbols": [],
            "timeRange": None,
            "dataRequirements": capability["dataEndpoints"],
            "analysisSteps": [],
            "visualization": visualization,
            "expectedArtifacts": capability["expectedArtifacts"],
            "validationRules": capability["validationRules"],
            "createdAt": timestamp,
            "updatedAt": timestamp,
        }
        for path, value in (
            (".data-agent/workspace.json", workspace),
            (".data-agent/profile.json", selection),
            (".data-agent/finance-run-plan.json", plan),
        ):
            write_atomic(
                stage, path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
            )
        write_atomic(stage, ".data-agent/events.jsonl", b"")
        skills = list(dict.fromkeys([*capability["requiredSkills"], "platform-ui-product-design"]))
        await deploy_skills(
            stage,
            root=self.root,
            skill_ids=skills,
            binding_workspace=target,
            fresh_project=True,
            deployment_id=attempt,
        )
        return {
            "llm": llm,
            "quant": self.catalog.quant_settings(capability["id"]),
            "dataAgent": {
                "profileId": application["profile"]["id"],
                "profileVersion": application["profile"]["version"],
                "capabilityId": capability["id"],
                "compositionSha256": application["composition"]["sha256"],
            },
        }

    async def rollback(self, target, attempt):
        catalog = SkillCatalogue(self.root)
        async with catalog.lock():
            state = catalog.read()
            key = catalog.deployment_key(target, "pi-agent")
            if state["deployments"].get(key, {}).get("id") == attempt:
                del state["deployments"][key]
                catalog.event(
                    state,
                    "deployment.initialization_rollback",
                    "project-initialization",
                    target="pi-agent",
                )
                catalog.commit(state)
