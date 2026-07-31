import argparse
import ast
import asyncio
import hashlib
import json
import os
import subprocess
import sys

from .config import ROOT
from .skill_catalog import SkillCatalogue
from .skill_compiler import compile_skills, metadata
from .skill_integrity import hash_directory, read_regular, verify_source_catalog
from .skill_publication import package_skill
from .skill_source import source_files, validate_complete


def check_catalog(root=ROOT, *, scripts=False):
    registry, lock, capsules = metadata(root)
    verified = verify_source_catalog(root)
    script_paths = []
    for definition in registry["coreSkills"]:
        identity = definition["id"]
        source = root / ".pi/skills" / identity
        validate_complete(source, definition)
        package = root / lock["skills"][identity]["packagePath"]
        if (
            hashlib.sha256(read_regular(package, 5242880)).hexdigest()
            != lock["skills"][identity]["packageSha256"]
        ):
            raise ValueError("Skill package hash mismatch: " + identity)
        for path in source_files(source):
            if path.suffix == ".py":
                ast.parse(path.read_text(), filename=str(path))
                if "scripts" in path.relative_to(source).parts:
                    script_paths.append(path)
    for phase in sorted(
        {phase for capsule in capsules["skills"].values() for phase in capsule["phases"]}
    ):
        compile_skills(root, phase=phase, budget=32000)
    if scripts:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "SystemRoot"}
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        for path in script_paths:
            if not path.read_text().startswith("#!/usr/bin/env python3\n"):
                raise ValueError(
                    "Skill Python script requires the python3 shebang: "
                    + str(path.relative_to(root))
                )
            result = subprocess.run(
                [sys.executable, "-B", str(path), "--help"],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode or b"usage:" not in result.stdout.lower():
                raise ValueError("Skill script must support --help: " + str(path.relative_to(root)))
        result = subprocess.run(
            [sys.executable, "-B", "tests/skills/test_contracts.py"],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=150,
            check=False,
        )
        if result.returncode:
            raise ValueError(
                "Skill behavioral contracts failed: "
                + result.stderr.decode("utf-8", "replace")[-12000:]
            )
    return {"skills": len(verified), "scripts": len(script_paths), "behavioralChecks": scripts}


async def package_catalog(root=ROOT):
    catalog = SkillCatalogue(root)
    packaged = skipped = 0
    async with catalog.lock():
        registry, lock, _ = metadata(root)
        for definition in registry["coreSkills"]:
            identity = definition["id"]
            expected = lock["skills"][identity]
            source = hash_directory(root / ".pi/skills" / identity)
            current = root / expected["packagePath"]
            if (
                expected["version"] == definition["version"]
                and expected["sourceSha256"] == source["hash"]
                and hashlib.sha256(read_regular(current, 5242880)).hexdigest()
                == expected["packageSha256"]
            ):
                skipped += 1
                continue
            package_skill(root, identity)
            packaged += 1
    return {"packaged": packaged, "unchanged": skipped}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "package"))
    parser.add_argument("--scripts", action="store_true")
    parser.add_argument("--check-lock", action="store_true")
    args = parser.parse_args()
    value = (
        check_catalog(scripts=args.scripts)
        if args.command == "check"
        else asyncio.run(package_catalog())
    )
    print(json.dumps(value, ensure_ascii=False))
