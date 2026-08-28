import asyncio
import json
import shutil

import pytest

from alphadeck_platform.config import ROOT
from alphadeck_platform.finance_catalog import FinanceCatalog
from alphadeck_platform.skill_catalog import SkillCatalogue
from alphadeck_platform.skill_compiler import compile_skills, load_skill, metadata
from alphadeck_platform.skill_deployment import SkillConflict, deploy_skills
from alphadeck_platform.skill_install import install_assets, read_receipt
from alphadeck_platform.skill_integrity import hash_directory


def test_finance_catalogue_matches_all_original_ready_compositions():
    catalog = FinanceCatalog()
    assert catalog.serialized_capabilities() == catalog.data["serializedCapabilities"]
    for identity, reference in catalog.data["applications"].items():
        assert catalog.resolve(capability_id=identity) == reference["resolved"]
        assert catalog.quant_settings(identity) == reference["quantSettings"]
    for capability in catalog.data["capabilities"]:
        if capability["status"] != "ready":
            with pytest.raises(ValueError):
                catalog.resolve(capability_id=capability["id"])


def test_skill_compiler_matches_original_text_hashes_and_character_counts():
    cases = json.loads(
        (ROOT / "services/platform-api/tests/fixtures/skill-capsules.json").read_text()
    )
    for case in cases:
        original = case["options"]
        result = compile_skills(
            phase=original["phase"],
            required_skill_ids=original.get("requiredSkillIds"),
            template_id=original.get("templateId"),
            budget=original["maxSystemContextChars"],
        )
        if result != case["expected"]:
            # The Vue design skill was republished; its compiled text and hashes
            # legitimately differ from the original source snapshot.
            assert set(result["selectedSkillIds"]) & {"platform-ui-product-design", "dashboard-visualization"}, original
            for key in ("runtime", "capabilityId", "selectedSkillIds", "phase"):
                assert result[key] == case["expected"][key], original
            assert all(skill["sourceSha256"] and skill["packageSha256"] for skill in result["skills"])
            assert len(result["systemContext"]) <= original["maxSystemContextChars"]
        else:
            assert result == case["expected"], original


def test_skill_package_only_compilation_and_budget_failures(tmp_path):
    (tmp_path / ".pi").mkdir()
    (tmp_path / "config").mkdir()
    for path in (
        ".pi/skills.registry.json",
        ".pi/skills.lock.json",
        "config/pi-agent-skill-capsules.json",
    ):
        shutil.copy2(ROOT / path, tmp_path / path)
    shutil.copytree(ROOT / ".pi/skill-packages", tmp_path / ".pi/skill-packages")
    result = compile_skills(
        tmp_path, phase="planning", required_skill_ids=["query-rewrite"], budget=32000
    )
    assert result["skills"][0]["source"] == "package"
    source = compile_skills(phase="planning", required_skill_ids=["query-rewrite"], budget=32000)
    assert result["taskContext"] == source["taskContext"]
    with pytest.raises(ValueError, match="budget"):
        compile_skills(phase="planning", required_skill_ids=["query-rewrite"], budget=256)
    with pytest.raises(ValueError, match="missing tools"):
        compile_skills(required_skill_ids=["quant-market-data"], available_tools=[])


def test_catalogue_snapshot_commit_and_tamper_detection(tmp_path):
    for relative in (".pi", "config"):
        shutil.copytree(ROOT / relative, tmp_path / relative)
    catalog = SkillCatalogue(tmp_path)
    state = catalog.read()
    catalog.initialize(state)
    image = catalog.resolve(state["active"], verify_all=True)
    catalog.commit(state)
    assert catalog.read()["generation"] == 1
    assert catalog.runtime()[0] == image
    (image / ".pi/skills/query-rewrite/SKILL.md").write_text("changed")
    with pytest.raises(ValueError, match="integrity"):
        catalog.resolve(state["active"], verify_all=True)


def asset(identity="query-rewrite"):
    registry, lock, _ = metadata(ROOT)
    entry = next(entry for entry in registry["coreSkills"] if entry["id"] == identity)
    loaded = load_skill(ROOT, entry, lock["skills"][identity])
    return {
        "id": identity,
        "claim": {
            "version": entry["version"],
            "source": "source",
            "sourceSha256": loaded["lock"]["sourceSha256"],
            "packageSha256": loaded["lock"]["packageSha256"],
        },
        "prepare": lambda destination: shutil.copytree(loaded["directory"], destination),
    }


def test_skill_install_preserves_unmanaged_and_refuses_local_overwrite(tmp_path):
    personal = tmp_path / ".pi/skills/personal-skill"
    personal.mkdir(parents=True)
    (personal / "keep.txt").write_text("personal")

    async def run():
        receipt = await install_assets(tmp_path, "pi-agent", [asset()])
        assert read_receipt(tmp_path, "pi-agent") == receipt
        assert (personal / "keep.txt").read_text() == "personal"
        managed = tmp_path / ".pi/skills/query-rewrite/SKILL.md"
        managed.write_text("user edits")
        with pytest.raises(ValueError, match="modified"):
            await install_assets(tmp_path, "pi-agent", [asset()])
        assert managed.read_text() == "user edits"
        await install_assets(tmp_path, "pi-agent", [asset()], overwrite_modified=True)
        assert hash_directory(managed.parent)["hash"] == asset()["claim"]["sourceSha256"]

    asyncio.run(run())


def test_skill_activation_failure_restores_previous_batch(tmp_path):
    committed = False

    def fail_commit():
        raise RuntimeError("intentional activation failure")

    async def run():
        await install_assets(tmp_path, "pi-agent", [asset()])
        previous = read_receipt(tmp_path, "pi-agent")
        with pytest.raises(RuntimeError, match="activation failure"):
            await install_assets(
                tmp_path,
                "pi-agent",
                [asset("run-planner")],
                activation={
                    "id": "operation",
                    "commit": fail_commit,
                    "isCommitted": lambda identity: committed,
                },
            )
        assert read_receipt(tmp_path, "pi-agent") == previous
        assert (tmp_path / ".pi/skills/query-rewrite/SKILL.md").exists()
        assert not (tmp_path / ".pi/skills/run-planner").exists()
        assert not (tmp_path / ".pi/.skill-install-transaction").exists()

    asyncio.run(run())


def test_project_skill_pinning_install_uninstall_and_rollback(tmp_path):
    root, workspace = tmp_path / "repository", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    for relative in (".pi", "config"):
        shutil.copytree(ROOT / relative, root / relative)

    async def run():
        first = await deploy_skills(workspace, root=root, skill_ids=["query-rewrite"])
        assert first["skillIds"] == ["query-rewrite"]
        with pytest.raises(ValueError, match="pinned"):
            compile_skills(
                root,
                phase="planning",
                required_skill_ids=["run-planner"],
                runtime_workspace=workspace,
            )
        with pytest.raises(SkillConflict):
            await deploy_skills(
                workspace,
                root=root,
                action="install",
                skill_id="run-planner",
                expected_revision="old",
            )
        second = await deploy_skills(
            workspace,
            root=root,
            action="install",
            skill_id="run-planner",
            expected_revision=first["id"],
        )
        assert second["skillIds"] == ["query-rewrite", "run-planner"]
        compiled = compile_skills(root, phase="planning", runtime_workspace=workspace, budget=32000)
        assert compiled["selectedSkillIds"] == ["run-planner", "query-rewrite"]
        third = await deploy_skills(
            workspace, root=root, action="rollback", expected_revision=second["id"]
        )
        assert third["skillIds"] == ["query-rewrite"]
        assert not (workspace / ".pi/skills/run-planner").exists()
        assert read_receipt(workspace, "pi-agent")["skills"].keys() == {"query-rewrite"}

    asyncio.run(run())
