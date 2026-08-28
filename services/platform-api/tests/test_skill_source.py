import io
import shutil
import stat
import tarfile
import zipfile

import pytest

from alphadeck_platform.config import ROOT
from alphadeck_platform.skill_archives import unpack_archive
from alphadeck_platform.skill_compiler import metadata
from alphadeck_platform.skill_source import (
    agent_metadata,
    normalize_path,
    read_source,
    save_source,
    source_files,
    validate_complete,
)


@pytest.mark.parametrize("definition", metadata(ROOT)[0]["coreSkills"], ids=lambda item: item["id"])
def test_native_source_validation_accepts_all_current_complete_skills(definition):
    validate_complete(ROOT / ".pi/skills" / definition["id"], definition)


def test_source_edits_are_bounded_and_preserve_executable_scripts(tmp_path):
    identity = "query-rewrite"
    directory = tmp_path / ".pi/skills" / identity
    shutil.copytree(ROOT / ".pi/skills" / identity, directory)
    original = read_source(tmp_path, identity)
    changed = save_source(
        tmp_path, identity, "SKILL.md", original["content"] + "\n# Additional guidance\n"
    )
    assert changed["skillMd"].endswith("# Additional guidance\n")
    script = save_source(
        tmp_path, identity, "scripts/additional.py", '#!/usr/bin/env python3\nprint("test")'
    )
    assert script["filePath"] == "scripts/additional.py"
    assert (directory / script["filePath"]).stat().st_mode & 0o111
    with pytest.raises(ValueError):
        save_source(tmp_path, identity, "references/bad.json", "{bad}")
    with pytest.raises(ValueError):
        save_source(tmp_path, identity, "README.md", "# auxiliary")
    with pytest.raises(ValueError):
        save_source(tmp_path, identity, "large.txt", "a" * (512 * 1024 + 1))
    target = directory / "references/escape.md"
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        source_files(directory)


@pytest.mark.parametrize("value", ["../escape", "a//b", "a/./b", "a/../b", "C:/file", "\0", "/"])
def test_skill_paths_reject_escapes_and_ambiguous_segments(value):
    with pytest.raises(ValueError):
        normalize_path(value)


def test_agent_yaml_validates_quoted_fields_and_rejects_duplicates():
    source = (ROOT / ".pi/skills/query-rewrite/agents/openai.yaml").read_text()
    assert agent_metadata(source, "query-rewrite")["default_prompt"]
    with pytest.raises(ValueError):
        agent_metadata(source + source, "query-rewrite")
    with pytest.raises(ValueError):
        agent_metadata(source, "different-skill")


@pytest.mark.parametrize("kind", ["tar", "zip"])
def test_native_archive_loader_accepts_regular_files_and_rejects_paths_and_links(tmp_path, kind):
    def packed(name, *, link=False):
        output = io.BytesIO()
        if kind == "tar":
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                item = tarfile.TarInfo(name)
                if link:
                    item.type, item.linkname = tarfile.SYMTYPE, "../outside"
                    archive.addfile(item)
                else:
                    item.size = 4
                    archive.addfile(item, io.BytesIO(b"test"))
        else:
            with zipfile.ZipFile(output, "w") as archive:
                item = zipfile.ZipInfo(name)
                item.external_attr = (stat.S_IFLNK if link else stat.S_IFREG | 0o644) << 16
                archive.writestr(item, b"test")
        return output.getvalue()

    filename = "fixture.tgz" if kind == "tar" else "fixture.zip"
    root = unpack_archive(packed("skill/SKILL.md"), filename, tmp_path / "valid", "skill")
    assert (root / "SKILL.md").read_text() == "test"
    for index, name in enumerate(
        ("../SKILL.md", "/SKILL.md", "skill/../SKILL.md", "skill\\SKILL.md")
    ):
        with pytest.raises(ValueError):
            unpack_archive(packed(name), filename, tmp_path / str(index), "skill")
    with pytest.raises(ValueError):
        unpack_archive(packed("skill/SKILL.md", link=True), filename, tmp_path / "link", "skill")
