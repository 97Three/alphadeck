import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .scaffold import controlled_path
from .sessions import iso
from .skill_integrity import hash_directory, read_regular
from .workspace import write_atomic

EDITABLE = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN = {"README.md", "CHANGELOG.md", "INSTALLATION_GUIDE.md", "QUICK_REFERENCE.md"}
REQUIRED = {"references", "scripts", "agents"}
VIRTUAL = {"skill.definition.json", "skill.runtime.json"}


def normalize_path(value, *, folder=False):
    value = (value or ("" if folder else "SKILL.md")).replace("\\", "/").lstrip("/")
    value = (value.rstrip("/") if folder else value).strip()
    if (
        not value
        or "\0" in value
        or ":" in value.split("/")[0]
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("文件路径不安全。")
    return value


def editable(path):
    return path == "SKILL.md" or Path(path).suffix.lower() in EDITABLE


def source_files(root):
    hash_directory(root)
    return sorted(path for path in root.rglob("*") if path.is_file() and path.name != ".DS_Store")


def reference(path):
    return path.startswith("references/") and path.endswith(".md")


def script(path):
    return path.startswith("scripts/") and re.search(r"\.(?:py|js|mjs|sh)$", path) is not None


def strict_json(content):
    def reject(value):
        raise ValueError("Invalid JSON number: " + value)

    return json.loads(content, parse_constant=reject)


def validate_text(path, content):
    if not editable(path):
        raise ValueError("不支持在线编辑该文件类型：" + path)
    if Path(path).name in FORBIDDEN:
        raise ValueError("Skill 包中不允许创建 " + Path(path).name + "。")
    if len(content.encode()) > 512 * 1024:
        raise ValueError("文件超过 512KB，不适合在线编辑。")
    if path == "SKILL.md":
        if "#" not in content.strip():
            raise ValueError("SKILL.md 内容过短或缺少标题。")
        if not re.match(r"^---\n[\s\S]*?\n---\n", content.strip()):
            raise ValueError("SKILL.md 必须包含 YAML frontmatter。")
    if path.endswith(".json"):
        strict_json(content)


def agent_metadata(source, identity, *, quoted=True):
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        key: [
            (tag, pattern)
            for tag, pattern in values
            if tag not in ("tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:bool")
        ]
        for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), ["t", "f"]
    )

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            if key in result:
                raise ValueError("Agent YAML 包含重复字段。")
            result[key] = loader.construct_object(value_node)
        return result

    Loader.add_constructor("tag:yaml.org,2002:map", mapping)
    value = yaml.load(source, Loader=Loader)
    interface = value.get("interface") if isinstance(value, dict) else None
    if not isinstance(interface, dict):
        raise ValueError(identity + " 的 agents/openai.yaml 必须包含根级 interface 对象。")
    block = re.search(r"^interface:\s*(?:#.*)?\r?\n((?:^[ \t]+.*(?:\r?\n|$))*)", source, re.M)
    for field in ("display_name", "short_description", "default_prompt"):
        if not isinstance(interface.get(field), str) or not interface[field].strip():
            raise ValueError(identity + " 的 interface." + field + " 必须是非空字符串。")
        if quoted and (
            not block or not re.search(r"^  " + field + r':\s*".+"\s*$', block[1], re.M)
        ):
            raise ValueError(identity + " 的 interface." + field + " 必须使用双引号。")
    if not 25 <= len(interface["short_description"]) <= 64:
        raise ValueError(identity + " 的 interface.short_description 必须为 25–64 个字符。")
    if "$" + identity not in interface["default_prompt"]:
        raise ValueError(identity + " 的 interface.default_prompt 必须显式引用 $" + identity + "。")
    return interface


def validate_complete(root, definition):
    identity = definition["id"]
    files = source_files(root)
    relative = [path.relative_to(root).as_posix() for path in files]
    if any(Path(path).name in FORBIDDEN for path in relative):
        raise ValueError("Skill 包含不允许的辅助文件。")
    for directory in REQUIRED:
        path = controlled_path(root, directory)
        if not path.is_dir():
            raise ValueError("Skill 缺少必需目录：" + directory)
    references, scripts = (
        sorted(path for path in relative if reference(path)),
        sorted(path for path in relative if script(path)),
    )
    if not references or any(
        path.startswith("references/") and not reference(path) for path in relative
    ):
        raise ValueError("Skill 必须包含 references/*.md。")
    if not scripts or any(path.startswith("scripts/") and not script(path) for path in relative):
        raise ValueError("Skill 必须包含受支持的确定性脚本。")
    if (
        sorted(definition.get("references", [])) != references
        or sorted(definition.get("scripts", [])) != scripts
    ):
        raise ValueError("registry.references 和 registry.scripts 必须完整登记资源。")
    skill = read_regular(controlled_path(root, "SKILL.md"), 512 * 1024).decode()
    match = re.match(r"^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)", skill)
    if not match:
        raise ValueError("SKILL.md 缺少 YAML frontmatter。")
    lines = [line.strip() for line in match[1].splitlines() if line.strip()]
    pairs = [line.partition(":") for line in lines]
    fields = {key.strip(): value.strip().strip("\"'") for key, _, value in pairs}
    if (
        len(lines) != 2
        or set(fields) != {"name", "description"}
        or fields["name"] != identity
        or not fields["description"]
    ):
        raise ValueError("Skill frontmatter 必须只包含正确的 name 和 description。")
    for path in references:
        if "](" + path + ")" not in skill:
            raise ValueError("SKILL.md 必须直接链接 " + path)
    for path in scripts:
        if path not in skill or not (root / path).stat().st_mode & 0o111:
            raise ValueError("SKILL.md 必须说明可执行脚本：" + path)
    agent_metadata(
        read_regular(controlled_path(root, "agents/openai.yaml"), 512 * 1024).decode(), identity
    )


def read_source(root, identity, relative="SKILL.md"):
    relative = normalize_path(relative)
    directory = controlled_path(root, ".pi/skills/" + identity)
    source_files(directory)
    if not editable(relative):
        raise ValueError("该文件不是可在线编辑的文本文件。")
    target = controlled_path(directory, relative)
    content = read_regular(target, 512 * 1024).decode("utf-8", "replace")
    return {
        "skillId": identity,
        "filePath": relative,
        "content": content,
        **({"skillMd": content} if relative == "SKILL.md" else {}),
        "relativePath": ".pi/skills/" + identity + "/" + relative,
        "size": len(content.encode()),
        "updatedAt": iso(datetime.fromtimestamp(target.stat().st_mtime, UTC)),
        "editable": True,
    }


def save_source(root, identity, relative, content):
    relative = normalize_path(relative)
    directory = controlled_path(root, ".pi/skills/" + identity)
    source_files(directory)
    content = content.rstrip()
    validate_text(relative, content)
    controlled_path(directory, relative)
    target = write_atomic(directory, relative, (content + "\n").encode())
    if script(relative):
        target.chmod(0o755)
    return read_source(root, identity, relative)
