import json
import shutil

import pytest

from alphadeck_platform.config import ROOT
from alphadeck_platform.skill_integrity import hash_directory, verify_source_catalog


def test_all_existing_skill_sources_match_the_locked_hashes():
    results = verify_source_catalog()
    assert len(results) == 12
    assert all(result["fileCount"] > 0 for result in results.values())


def test_crlf_or_source_tampering_changes_the_same_hash(tmp_path):
    directory = tmp_path / "query-rewrite"
    shutil.copytree(ROOT / ".pi/skills/query-rewrite", directory)
    expected = json.loads((ROOT / ".pi/skills.lock.json").read_text())["skills"]["query-rewrite"][
        "sourceSha256"
    ]
    assert hash_directory(directory)["hash"] == expected
    skill = directory / "SKILL.md"
    skill.write_bytes(skill.read_bytes().replace(b"\n", b"\r\n"))
    assert hash_directory(directory)["hash"] != expected


def test_skill_tree_rejects_symlink_files(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("secret")
    directory = tmp_path / "skill"
    directory.mkdir()
    (directory / "alias").symlink_to(outside)
    with pytest.raises(ValueError, match="link or special"):
        hash_directory(directory)
