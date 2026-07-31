import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from .config import ROOT
from .provenance import js_json
from .scaffold import controlled_path
from .skill_archives import unpack_archive
from .skill_catalog import SkillCatalogue, checksum, compact, inventory, now, valid_skill
from .skill_compiler import metadata
from .skill_dashboard import dashboard, file_time
from .skill_deployment import SkillConflict, replace_skill
from .skill_integrity import read_regular
from .skill_publication import new_version, release_identity, validate_publication
from .skill_source import (
    VIRTUAL,
    normalize_path,
    read_source,
    reference,
    save_source,
    script,
    source_files,
    strict_json,
    validate_complete,
)
from .workspace import write_atomic


def revision(root):
    return checksum(compact({"schemaVersion": 1, "files": inventory(root)}))


def write_json(root, path, value):
    controlled_path(root, path)
    write_atomic(root, path, (js_json(value, indent=2) + "\n").encode())


def text_difference(previous, current):
    old, new = (
        [] if previous is None else previous.split("\n"),
        [] if current is None else current.split("\n"),
    )
    old_counts, new_counts = Counter(old), Counter(new)
    added = sum((new_counts - old_counts).values())
    removed = sum((old_counts - new_counts).values())
    preview = []
    for index in range(max(len(old), len(new))):
        if len(preview) >= 14:
            break
        left, right = (
            old[index] if index < len(old) else None,
            new[index] if index < len(new) else None,
        )
        if left == right:
            continue
        if left is not None:
            preview.append("- " + left)
        if right is not None:
            preview.append("+ " + right)
    return (
        added,
        removed,
        preview
        or (
            ["二进制文件或文件过大，跳过文本预览。"]
            if previous is None and current is None
            else ["文件内容有变化。"]
        ),
    )


class SkillsAdministration:
    def __init__(self, root=ROOT, *, validator=validate_publication):
        self.catalog, self.validator = SkillCatalogue(Path(root)), validator

    def current(self, state, identity):
        if not valid_skill(identity):
            raise ValueError("skillId 不合法。")
        active = self.catalog.resolve(state["active"]) if state["active"] else self.catalog.root
        registry, _, _ = metadata(active)
        if not any(entry["id"] == identity for entry in registry["coreSkills"]):
            raise ValueError("技能未登记。")
        draft = state["drafts"].get(identity)
        editor = self.catalog.resolve(draft["revision"], verify_all=True) if draft else active
        return (
            active,
            editor,
            draft,
            draft["revision"] if draft else state["active"] or revision(active),
        )

    @staticmethod
    def expected(actual, expected):
        if not expected or actual != expected:
            raise SkillConflict(
                "技能已被其他操作修改，请刷新并重新确认差异；你的编辑内容仍保留在编辑器中。"
            )

    @staticmethod
    def virtual_content(root, identity, path):
        registry, _, capsules = metadata(root)
        value = (
            next(entry for entry in registry["coreSkills"] if entry["id"] == identity)
            if path == "skill.definition.json"
            else capsules["skills"][identity]
        )
        return js_json(value, indent=2) + "\n"

    def read_file(self, identity, path="SKILL.md"):
        _, editor, draft, selected = self.current(self.catalog.read(), identity)
        if path in VIRTUAL:
            content = self.virtual_content(editor, identity, path)
            result = {
                "skillId": identity,
                "filePath": path,
                "content": content,
                "relativePath": path,
                "size": len(content.encode()),
                "updatedAt": draft["updatedAt"] if draft else None,
                "editable": True,
            }
        else:
            result = read_source(editor, identity, path)
        return {**result, "revision": selected, "draft": draft is not None}

    async def edit_draft(self, identity, expected, actor, action, operation):
        async with self.catalog.lock():
            state = self.catalog.read()
            _, editor, draft, actual = self.current(state, identity)
            self.expected(actual, expected)
            self.catalog.initialize(state)
            stage = self.catalog.stage(editor)
            try:
                operation(stage)
                selected = self.catalog.seal(stage)
                state["drafts"][identity] = {
                    "revision": selected,
                    "base": draft["base"] if draft else state["active"],
                    "actor": actor,
                    "updatedAt": now(),
                }
                self.catalog.event(state, action, actor, skillId=identity)
                self.catalog.commit(state)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)

    async def save_file(self, identity, path, content, *, expected, actor):
        def edit(stage):
            if path not in VIRTUAL:
                save_source(stage, identity, path, content)
                return
            if len(content.encode()) > 524288:
                raise ValueError("文件超过 512KB。")
            value = strict_json(content)
            if not isinstance(value, dict):
                raise ValueError("技能配置必须为 JSON 对象。")
            registry, _, capsules = metadata(stage)
            if path == "skill.definition.json":
                previous = next(
                    entry for entry in registry["coreSkills"] if entry["id"] == identity
                )
                if value.get("id") != identity or value.get("version") != previous["version"]:
                    raise ValueError("技能 ID 不可更改；版本号请在发布时设置。")
                registry["coreSkills"] = [
                    value if entry["id"] == identity else entry for entry in registry["coreSkills"]
                ]
                write_json(stage, ".pi/skills.registry.json", registry)
            else:
                capsules["skills"][identity] = value
                write_json(stage, "config/pi-agent-skill-capsules.json", capsules)
            metadata(stage)

        await self.edit_draft(identity, expected, actor, "draft.save", edit)
        return self.read_file(identity, path)

    def studio(self):
        state = self.catalog.read()
        active = self.catalog.resolve(state["active"]) if state["active"] else self.catalog.root
        result = dashboard(active)
        selected = state["active"] or revision(active)
        for item in result["skills"]:
            identity, draft = item["id"], state["drafts"].get(item["id"])
            editor = self.catalog.resolve(draft["revision"]) if draft else active
            if draft:
                item["source"] = next(
                    entry for entry in dashboard(editor)["skills"] if entry["id"] == identity
                )["source"]
            for path in sorted(VIRTUAL):
                content = self.virtual_content(editor, identity, path)
                item["source"]["files"].append(
                    {
                        "path": path,
                        "name": path,
                        "kind": "other",
                        "editable": True,
                        "size": len(content.encode()),
                        "updatedAt": draft["updatedAt"] if draft else None,
                        "sha256": "",
                        "sha256Short": None,
                    }
                )
            item["editing"] = {
                "revision": draft["revision"] if draft else selected,
                "hasDraft": draft is not None,
                "updatedBy": draft["actor"] if draft else None,
            }
            for release in item["changelog"]["releases"]:
                complete = next(
                    (
                        entry
                        for entry in state["releases"]
                        if entry["skillId"] == identity and entry["version"] == release["version"]
                    ),
                    None,
                )
                release["completeSnapshot"] = bool(
                    complete or not state["active"] and release["version"] == item["version"]
                )
                release["actor"] = complete["actor"] if complete else release.get("actor")
        return {
            **result,
            "catalogRevision": selected,
            "activity": list(reversed(state["events"][-100:])),
        }

    async def discard(self, identity, *, expected, actor):
        async with self.catalog.lock():
            state = self.catalog.read()
            _, _, _, actual = self.current(state, identity)
            self.expected(actual, expected)
            state["drafts"].pop(identity, None)
            self.catalog.event(state, "draft.discard", actor, skillId=identity)
            self.catalog.commit(state)
        return self.studio()

    async def file_mutation(self, identity, path, action, *, expected, actor):
        folder = action in ("createSkillFolder", "deleteSkillFolder")
        relative = normalize_path(path, folder=folder)
        if (
            relative in VIRTUAL
            or not folder
            and relative in ("SKILL.md", "agents/openai.yaml")
            or folder
            and action == "deleteSkillFolder"
            and relative in ("references", "scripts", "agents")
        ):
            raise ValueError("不能删除 Skill 的必需入口或目录。")

        def edit(stage):
            directory = controlled_path(stage, ".pi/skills/" + identity)
            files = source_files(directory)
            target = controlled_path(directory, relative)
            if action == "createSkillFolder":
                target.mkdir(parents=True, exist_ok=True)
                return
            if not target.exists() or target.is_dir() != folder:
                raise ValueError("文件或文件夹不存在。")
            remaining = [
                file.relative_to(directory).as_posix()
                for file in files
                if file != target and target not in file.parents
            ]
            if not any(reference(item) for item in remaining) or not any(
                script(item) for item in remaining
            ):
                raise ValueError("不能删除最后一个 reference 或确定性 script。")
            if folder:
                shutil.rmtree(target)
            else:
                target.unlink()

        await self.edit_draft(identity, expected, actor, "draft." + action, edit)
        return self.studio()

    async def upload(self, identity, content, filename, *, expected, actor):
        def edit(stage):
            with tempfile.TemporaryDirectory(prefix="qp-skill-upload-") as temporary:
                source = unpack_archive(content, filename, Path(temporary), identity)
                destination = controlled_path(stage, ".pi/skills/" + identity)
                shutil.rmtree(destination)
                shutil.copytree(source, destination)
                registry, _, _ = metadata(stage)
                validate_complete(
                    destination,
                    next(entry for entry in registry["coreSkills"] if entry["id"] == identity),
                )

        await self.edit_draft(identity, expected, actor, "draft.upload", edit)
        return self.studio()

    async def publish(self, identity, version, summary, changes, *, expected, actor):
        async with self.catalog.lock():
            state = self.catalog.read()
            active, editor, draft, actual = self.current(state, identity)
            self.expected(actual, expected)
            if draft is None:
                raise ValueError("请先保存草稿并确认差异。")
            if release_identity(self.catalog.resolve(draft["base"]), identity) != release_identity(
                active, identity
            ):
                raise SkillConflict(
                    "草稿基于旧的已发布版本；请保留修改后放弃旧草稿，再基于当前版编辑。"
                )
            if (
                not summary.strip()
                or not changes
                or any(not isinstance(item, str) or not item.strip() for item in changes)
            ):
                raise ValueError("发布摘要和变更点不能为空。")
            history = next(
                entry for entry in dashboard(active)["skills"] if entry["id"] == identity
            )["changelog"]["releases"]
            new_version(
                version,
                [entry["version"] for entry in history]
                + [entry["version"] for entry in state["releases"] if entry["skillId"] == identity],
            )
            stage = self.catalog.stage(active)
            try:
                replace_skill(stage, editor, identity)
                registry, _, _ = metadata(stage)
                next(entry for entry in registry["coreSkills"] if entry["id"] == identity)[
                    "version"
                ] = version
                write_json(stage, ".pi/skills.registry.json", registry)
                changelog = json.loads(read_regular(stage / ".pi/skills.changelog.json", 2097152))
                changelog["skills"][identity]["releases"].insert(
                    0,
                    {
                        "version": version,
                        "date": now(),
                        "summary": summary.strip(),
                        "changes": changes,
                        "actor": actor,
                    },
                )
                write_json(stage, ".pi/skills.changelog.json", changelog)
                await self.validator(stage, identity, package_source=True)
                state["active"] = self.catalog.seal(stage)
                state["releases"].append(
                    {
                        "skillId": identity,
                        "version": version,
                        "revision": state["active"],
                        "actor": actor,
                        "date": now(),
                    }
                )
                del state["drafts"][identity]
                self.catalog.event(
                    state, "release.publish", actor, skillId=identity, version=version
                )
                self.catalog.commit(state)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        return self.studio()

    async def rollback(self, identity, version, *, expected, actor):
        async with self.catalog.lock():
            state = self.catalog.read()
            active, _, draft, actual = self.current(state, identity)
            self.expected(actual, expected)
            if draft:
                raise SkillConflict("请先发布或放弃草稿，再回退已发布版本。")
            self.catalog.initialize(state)
            release = next(
                (
                    entry
                    for entry in state["releases"]
                    if entry["skillId"] == identity and entry["version"] == version
                ),
                None,
            )
            if release is None:
                raise ValueError("此历史版本缺少完整运行规则快照，不能安全回退。")
            stage = self.catalog.stage(active)
            try:
                replace_skill(
                    stage, self.catalog.resolve(release["revision"], verify_all=True), identity
                )
                await self.validator(stage, identity, package_source=False)
                state["active"] = self.catalog.seal(stage)
                self.catalog.event(
                    state, "release.rollback", actor, skillId=identity, version=version
                )
                self.catalog.commit(state)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        return self.studio()

    def diff(self, identity):
        active, editor, _, selected = self.current(self.catalog.read(), identity)
        registry, _, _ = metadata(editor)
        definition = next(entry for entry in registry["coreSkills"] if entry["id"] == identity)
        directory = registry.get("policy", {}).get("packageDir", ".pi/skill-packages")
        choices = [
            directory + "/versions/" + identity + "/" + definition["version"] + ".tgz",
            directory + "/" + identity + ".tgz",
        ]
        base_path = next(
            (path for path in choices if controlled_path(editor, path).is_file()), None
        )
        source = controlled_path(editor, ".pi/skills/" + identity)
        with tempfile.TemporaryDirectory(prefix="qp-skill-diff-") as temporary:
            old_root = (
                unpack_archive(
                    read_regular(editor / base_path, 5242880), "base.tgz", Path(temporary), identity
                )
                if base_path
                else None
            )
            previous = (
                {path.relative_to(old_root).as_posix(): path for path in source_files(old_root)}
                if old_root
                else {}
            )
            current = {path.relative_to(source).as_posix(): path for path in source_files(source)}
            files = []
            for path in sorted(previous.keys() | current.keys()):
                old, new = previous.get(path), current.get(path)
                left, right = (
                    read_regular(old, 4194304) if old else None,
                    read_regular(new, 4194304) if new else None,
                )
                if left == right:
                    continue
                left_text, right_text = (
                    left.decode("utf-8", "replace")
                    if left is not None and len(left) <= 524288
                    else None,
                    right.decode("utf-8", "replace")
                    if right is not None and len(right) <= 524288
                    else None,
                )
                added, removed, preview = text_difference(left_text, right_text)
                files.append(
                    {
                        "path": path,
                        "status": "modified" if old and new else "deleted" if old else "added",
                        "previousSize": len(left) if left is not None else None,
                        "currentSize": len(right) if right is not None else None,
                        "previousUpdatedAt": file_time(old) if old else None,
                        "currentUpdatedAt": file_time(new) if new else None,
                        "addedLines": added,
                        "removedLines": removed,
                        "preview": preview,
                    }
                )
        totals = {
            **{
                status: sum(entry["status"] == status for entry in files)
                for status in ("added", "modified", "deleted")
            },
            "addedLines": sum(entry["addedLines"] for entry in files),
            "removedLines": sum(entry["removedLines"] for entry in files),
        }
        for path in sorted(VIRTUAL):
            left, right = (
                self.virtual_content(active, identity, path),
                self.virtual_content(editor, identity, path),
            )
            if left != right:
                files.append(
                    {
                        "path": path,
                        "status": "modified",
                        "previousSize": len(left.encode()),
                        "currentSize": len(right.encode()),
                        "previousUpdatedAt": None,
                        "currentUpdatedAt": None,
                        "addedLines": len(right.split("\n")),
                        "removedLines": len(left.split("\n")),
                        "preview": ["- " + left, "+ " + right],
                    }
                )
                totals["modified"] += 1
        return {
            "skillId": identity,
            "baseVersion": definition["version"],
            "basePackagePath": base_path,
            "changed": bool(files),
            "files": files,
            "totals": totals,
            "revision": selected,
        }
