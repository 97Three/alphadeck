import asyncio
import shutil

import pytest

from alphadeck_platform.config import ROOT
from alphadeck_platform.skill_administration import SkillsAdministration
from alphadeck_platform.skill_catalog import CONTENT
from alphadeck_platform.skill_compiler import metadata
from alphadeck_platform.skill_deployment import SkillConflict
from alphadeck_platform.skill_integrity import verify_source_catalog
from alphadeck_platform.skill_publication import package_skill


@pytest.fixture
def skills_root(tmp_path):
    for relative in CONTENT:
        source, target = ROOT / relative, tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    return tmp_path


async def fixture_validator(stage, identity, *, package_source):
    if package_source:
        package_skill(stage, identity)
    verify_source_catalog(stage)


def test_skill_drafts_have_compare_and_swap_revision_and_never_activate_early(skills_root):
    admin = SkillsAdministration(skills_root, validator=fixture_validator)

    async def run():
        before = admin.read_file("query-rewrite")
        content = before["content"] + "\n# Test additional guidance\n"
        results = await asyncio.gather(
            *(
                admin.save_file(
                    "query-rewrite",
                    "SKILL.md",
                    content + str(index),
                    expected=before["revision"],
                    actor="fixture",
                )
                for index in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, SkillConflict) for item in results) == 1
        assert (skills_root / ".pi/skills/query-rewrite/SKILL.md").read_text() == before["content"]
        state = admin.catalog.read()
        assert state["active"] != state["drafts"]["query-rewrite"]["revision"]
        active = admin.catalog.resolve(state["active"])
        assert (active / ".pi/skills/query-rewrite/SKILL.md").read_text() == before["content"]
        diff = admin.diff("query-rewrite")
        assert diff["changed"] and diff["totals"]["modified"] == 1
        discarded = await admin.discard(
            "query-rewrite", expected=admin.read_file("query-rewrite")["revision"], actor="fixture"
        )
        assert not next(item for item in discarded["skills"] if item["id"] == "query-rewrite")[
            "editing"
        ]["hasDraft"]
        assert admin.read_file("query-rewrite")["content"] == before["content"]

    asyncio.run(run())


def test_skill_publication_and_rollback_pin_complete_runtime_and_sources(skills_root):
    admin = SkillsAdministration(skills_root, validator=fixture_validator)

    async def run():
        original = admin.read_file("query-rewrite")
        version = next(
            item for item in metadata(skills_root)[0]["coreSkills"] if item["id"] == "query-rewrite"
        )["version"]
        major, minor, patch = map(int, version.split("."))
        next_version = f"{major}.{minor}.{patch + 1}"
        saved = await admin.save_file(
            "query-rewrite",
            "SKILL.md",
            original["content"] + "\n# Published change\n",
            expected=original["revision"],
            actor="fixture",
        )
        published = await admin.publish(
            "query-rewrite",
            next_version,
            "Test release",
            ["Add guidance"],
            expected=saved["revision"],
            actor="fixture",
        )
        current = next(item for item in published["skills"] if item["id"] == "query-rewrite")
        assert current["version"] == next_version and current["health"]["status"] == "ok"
        assert current["changelog"]["currentRelease"]["completeSnapshot"]
        assert not current["editing"]["hasDraft"]
        assert admin.catalog.read()["releases"][-1]["revision"] == published["catalogRevision"]
        rolled = await admin.rollback(
            "query-rewrite", version, expected=current["editing"]["revision"], actor="fixture"
        )
        assert (
            next(item for item in rolled["skills"] if item["id"] == "query-rewrite")["version"]
            == version
        )
        assert admin.read_file("query-rewrite")["content"] == original["content"]
        verify_source_catalog(admin.catalog.resolve(rolled["catalogRevision"]))

    asyncio.run(run())


def test_rejected_skill_publication_retains_active_release_and_editable_draft(skills_root):
    async def fail(stage, identity, **_):
        raise ValueError("controlled contract failure")

    admin = SkillsAdministration(skills_root, validator=fail)

    async def run():
        source = admin.read_file("query-rewrite")
        saved = await admin.save_file(
            "query-rewrite",
            "SKILL.md",
            source["content"] + "\n# Draft only\n",
            expected=source["revision"],
            actor="fixture",
        )
        before = admin.catalog.read()
        with pytest.raises(ValueError, match="controlled contract failure"):
            await admin.publish(
                "query-rewrite",
                "999.0.0",
                "Test rejection",
                ["Test"],
                expected=saved["revision"],
                actor="fixture",
            )
        assert admin.catalog.read() == before
        assert admin.read_file("query-rewrite")["draft"]
        with pytest.raises(ValueError):
            await admin.file_mutation(
                "query-rewrite",
                "SKILL.md",
                "deleteSkillFile",
                expected=saved["revision"],
                actor="fixture",
            )

    asyncio.run(run())


def test_real_native_skill_publication_executes_original_offline_behavior_contracts_in_isolation(
    skills_root,
):
    admin = SkillsAdministration(skills_root)

    async def run():
        original = admin.read_file("query-rewrite")
        saved = await admin.save_file(
            "query-rewrite",
            "SKILL.md",
            original["content"] + "\n# Checked guidance\n",
            expected=original["revision"],
            actor="fixture",
        )
        published = await admin.publish(
            "query-rewrite",
            "999.0.0",
            "Verified fixture release",
            ["Exercise native publisher"],
            expected=saved["revision"],
            actor="fixture",
        )
        item = next(item for item in published["skills"] if item["id"] == "query-rewrite")
        assert item["version"] == "999.0.0" and item["health"]["status"] == "ok"

    asyncio.run(run())
