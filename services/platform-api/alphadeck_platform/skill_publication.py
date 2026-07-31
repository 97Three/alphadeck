import ast
import gzip
import io
import re
import shutil
import tarfile

from .config import ROOT
from .generated_sandbox import wrap_command
from .provenance import js_json
from .scaffold import controlled_path
from .skill_catalog import checksum
from .skill_compiler import compile_skills, metadata
from .skill_integrity import hash_directory, verify_source_catalog
from .skill_source import source_files, validate_complete
from .validation_runtime import GeneratedCommandRunner
from .workspace import write_atomic


def new_version(version, previous):
    if (
        not isinstance(version, str)
        or len(version) > 32
        or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
    ):
        raise ValueError("版本号必须为规范的 major.minor.patch。")
    current = tuple(int(part) for part in version.split("."))
    if any(current <= tuple(int(part) for part in item.split(".")) for item in previous):
        raise ValueError("新版本必须高于所有已发布版本；恢复旧版请使用回退。")


def release_identity(root, identity):
    registry, lock, capsules = metadata(root)
    definition = next(item for item in registry["coreSkills"] if item["id"] == identity)
    return checksum(
        js_json(
            {
                "definition": definition,
                "capsule": capsules["skills"][identity],
                "lock": lock["skills"][identity],
            }
        )
    )


def package_skill(stage, identity):
    registry, lock, _ = metadata(stage)
    definition = next(item for item in registry["coreSkills"] if item["id"] == identity)
    source = controlled_path(stage, ".pi/skills/" + identity)
    validate_complete(source, definition)
    stream = io.BytesIO()
    with (
        gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive,
    ):
        for path in [source, *sorted(source.rglob("*"))]:
            if path.name == ".DS_Store":
                continue
            info = archive.gettarinfo(
                str(path),
                arcname=identity
                + ("/" + path.relative_to(source).as_posix() if path != source else ""),
            )
            info.uid, info.gid, info.uname, info.gname, info.mtime = 0, 0, "", "", 0
            if path.is_file():
                with path.open("rb") as file:
                    archive.addfile(info, file)
            else:
                archive.addfile(info)
    content = stream.getvalue()
    if len(content) > 5242880:
        raise ValueError("技能包超过 5MB。")
    directory = (
        registry.get("policy", {}).get("packageDir", ".pi/skill-packages").replace("\\", "/")
    )
    path = directory + "/" + identity + ".tgz"
    controlled_path(stage, path)
    write_atomic(stage, path, content)
    actual = hash_directory(source)
    lock["skills"][identity] = {
        "version": definition["version"],
        "packagePath": path,
        "sourceSha256": actual["hash"],
        "packageSha256": checksum(content),
        "fileCount": actual["fileCount"],
    }
    write_atomic(stage, ".pi/skills.lock.json", (js_json(lock, indent=2) + "\n").encode())
    snapshot = directory + "/versions/" + identity + "/" + definition["version"] + ".tgz"
    target = controlled_path(stage, snapshot)
    if target.exists() and target.read_bytes() != content:
        raise ValueError("同一版本的技能包快照不能覆盖。")
    write_atomic(stage, snapshot, content)


async def validate_publication(stage, identity, *, package_source=True, code_root=ROOT):
    if package_source:
        package_skill(stage, identity)
    registry, _, capsules = metadata(stage)
    verify_source_catalog(stage)
    for definition in registry["coreSkills"]:
        source = stage / ".pi/skills" / definition["id"]
        validate_complete(source, definition)
        for path in source_files(source):
            if path.suffix == ".py":
                ast.parse(path.read_text(), filename=str(path))
    for phase in capsules["skills"][identity]["phases"]:
        compile_skills(stage, required_skill_ids=[identity], phase=phase, budget=32000)
    trusted = controlled_path(stage, "tests/skills/test_contracts.py")
    trusted.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(code_root / "tests/skills/test_contracts.py", trusted)
    try:
        runner = GeneratedCommandRunner(
            command_factory=lambda root, args: wrap_command(
                root,
                "env",
                ["PYTHONDONTWRITEBYTECODE=1", "python3", "-B", "tests/skills/test_contracts.py"],
                root=code_root,
            )
        )
        result = await runner.run(stage, [], timeout=150)
        if result["timedOut"] or result["exitCode"] != 0:
            raise ValueError("技能发布行为检查失败：" + result["output"][-12000:])
    finally:
        trusted.unlink(missing_ok=True)
        # These directories were created only for the trusted temporary checker.
        for path in (trusted.parent, trusted.parent.parent):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
