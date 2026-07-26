"""Platform owned Vue/Vite scaffold for generated research dashboards."""

import json
from pathlib import Path

from .config import ROOT
from .provenance import js_json
from .skill_integrity import read_regular
from .workspace import write_atomic

TEMPLATE_ROOT = ROOT / "config/vue-dashboard-template"
TEMPLATE_FILES = (
    "index.html",
    "vite.config.ts",
    "postcss.config.cjs",
    "tsconfig.json",
    "src/main.ts",
    "src/env.d.ts",
    "src/App.vue",
    "src/style.css",
)
CATALOG = {
    "files": {
        relative: (TEMPLATE_ROOT / relative).read_text(encoding="utf-8")
        for relative in TEMPLATE_FILES
    }
}


def controlled_path(root, relative):
    root = Path(root).resolve(strict=True)
    lexical = root / relative
    if lexical.resolve() != lexical or root not in lexical.parents:
        raise ValueError("Platform scaffold paths cannot contain links or escape the workspace")
    return lexical


def read(root, relative):
    try:
        return read_regular(controlled_path(root, relative), 2 * 1024 * 1024).decode(
            "utf-8", "replace"
        )
    except FileNotFoundError:
        return None


def write(root, relative, content):
    path = controlled_path(root, relative)
    encoded = content.encode()
    try:
        if read_regular(path, 2 * 1024 * 1024) == encoded:
            return
    except FileNotFoundError:
        pass
    write_atomic(root, relative, encoded)


def record(root, relative):
    try:
        value = json.loads(read(root, relative) or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def ensure_config(root):
    # The data import and market proxy are platform owned; generated code cannot alter them.
    for relative in ("vite.config.ts", "postcss.config.cjs"):
        content = CATALOG["files"][relative]
        if read(root, relative) != content:
            write(root, relative, content)


def ensure_scaffold(root, project, *, shared_modules=True, restore=False):
    root = Path(root).resolve(strict=True)
    defaults = json.loads((TEMPLATE_ROOT / "package.json").read_text(encoding="utf-8"))
    defaults["name"] = project
    package = record(root, "package.json") or defaults
    package["name"] = project
    package["scripts"] = {**(package.get("scripts") or {}), **defaults["scripts"]}
    for group in ("dependencies", "devDependencies"):
        current = package.get(group) or {}
        current = {key: value for key, value in current.items() if key not in {
            "next", "next-rspack", "react", "react-dom", "lucide-react", "recharts",
        }}
        package[group] = {**current, **defaults.get(group, {})}
    write(root, "package.json", js_json(package, indent=2) + "\n")
    ensure_config(root)
    for relative, content in CATALOG["files"].items():
        if relative in {"vite.config.ts", "postcss.config.cjs"}:
            continue
        if read(root, relative) is None or (restore and relative in {"src/App.vue", "src/style.css"}):
            write(root, relative, content)
    if shared_modules and not (root / "node_modules").exists():
        modules = ROOT / "web/node_modules"
        if (modules / "vite/package.json").is_file():
            (root / "node_modules").symlink_to(modules, target_is_directory=True)
