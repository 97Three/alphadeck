import json
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .config import ROOT
from .messages import js_length, js_slice
from .skill_integrity import read_regular

CATALOG = json.loads((ROOT / "config/artifact-policy-catalog.json").read_text())
REQUIRED = (
    ".data-agent/finance-run-plan.json",
    "src/App.vue",
    "data_file/final/dashboard-data.json",
    "evidence/sources.json",
    "evidence/data_quality.json",
)


def pattern(item):
    return re.compile(item["source"], re.ASCII | (re.I if "i" in item["flags"] else 0))


def snippet(value):
    compact = re.sub(r"\s+", " ", value).strip()
    return compact if js_length(compact) <= 180 else js_slice(compact, 0, 180) + "..."


def allowed_backend(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return (
            parsed.hostname in {"localhost", "127.0.0.1"}
            and port == 8000
            and parsed.path.startswith("/api/v1/")
        )
    except ValueError:
        return False


def remote_violations(relative, content):
    if relative == "vite.config.ts":
        from .scaffold import CATALOG as scaffold_catalog

        if content == scaffold_catalog["files"]["vite.config.ts"]:
            return []
    market = relative == "app/api/market/route.ts" or bool(
        re.fullmatch(r"app/api/market/.*/route\.ts", relative)
    )
    url_pattern = pattern(CATALOG["REMOTE_URL_PATTERN"])
    violations = []
    for usage in CATALOG["REMOTE_USAGE_PATTERNS"]:
        for match in list(pattern(usage["pattern"]).finditer(content))[:3]:
            urls = url_pattern.findall(match[0])
            if any(not (market and allowed_backend(url)) for url in urls):
                violations.append(f"{relative} 存在{usage['label']}：{snippet(match[0])}")
    for url in url_pattern.findall(content)[:8]:
        if market and allowed_backend(url):
            continue
        if re.search(r"nextjs\.org|react\.dev|vercel\.com", url, re.I) and re.search(
            r"package\.json$|next\.config\.", relative
        ):
            continue
        if relative != "package.json":
            violations.append(f"{relative} 存在外部 URL：{url}")
    return list(dict.fromkeys(violations))


def pattern_violations(relative, content, patterns):
    output = []
    for item in patterns:
        match = pattern(item["pattern"]).search(content)
        if match:
            output.append(f"{relative} 存在{item['label']}：{snippet(match[0])}")
    return output


def check_artifact_policy(root):
    root = Path(root).resolve(strict=True)
    files, count, total = [], 0, 0
    maximum = CATALOG["ARTIFACT_POLICY_MAX_FILE_BYTES"]

    def read(path, *, optional=True):
        nonlocal total
        try:
            resolved = path.resolve(strict=True)
            if root not in resolved.parents or path.is_symlink():
                raise ValueError(
                    "Artifact policy refuses symbolic links or paths outside the workspace"
                )
            data = read_regular(path, maximum)
            total += len(data)
            if total > 32 * 1024 * 1024:
                raise ValueError("Artifact policy exceeded its total source byte budget")
            return data.decode("utf-8", "replace")
        except FileNotFoundError:
            if optional:
                return None
            raise

    def visit(directory, depth=0):
        nonlocal count
        if depth > 64 or directory.is_symlink():
            raise ValueError("Artifact policy refuses deeply nested or linked source directories")
        for path in sorted(
            directory.iterdir(), key=lambda path: path.name.encode("utf-16-be", "surrogatepass")
        ):
            count += 1
            if count > 10000:
                raise ValueError("Artifact policy exceeded its source entry budget")
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("Artifact policy refuses symbolic links in executable source")
            if stat.S_ISDIR(info.st_mode):
                if path.name not in CATALOG["ARTIFACT_POLICY_SKIP_DIRS"]:
                    visit(path, depth + 1)
            elif path.suffix in CATALOG["ARTIFACT_POLICY_SOURCE_EXTENSIONS"]:
                if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                    raise ValueError("Artifact source exceeds the policy file budget")
                files.append(path)

    for relative in CATALOG["ARTIFACT_POLICY_ROOT_DIRS"]:
        directory = root / relative
        if directory.is_dir():
            visit(directory)
    for relative in CATALOG["ARTIFACT_POLICY_ROOT_FILES"]:
        path = root / relative
        if path.exists():
            files.append(path)
    files = list(dict.fromkeys(files))
    missing = [relative for relative in REQUIRED if not (root / relative).is_file()]
    violations = ["缺少标准产物：" + "、".join(missing) + "。"] if missing else []
    for path in files:
        relative = path.relative_to(root).as_posix()
        content = read(path)
        if not content:
            continue
        if relative.startswith(("app/api/", "src/app/api/", "pages/api/", "src/pages/api/")):
            violations.append(f"{relative} 包含生成的后端接口；API 应由 Python 服务执行。")
        violations.extend(remote_violations(relative, content))
        violations.extend(
            pattern_violations(relative, content, CATALOG["SENSITIVE_ARTIFACT_PATTERNS"])
        )
        if re.match(r"(?:app|components|hooks|lib|src)/", relative):
            violations.extend(
                pattern_violations(relative, content, CATALOG["EXECUTION_ESCAPE_PATTERNS"])
            )
            calls = re.findall(
                r"\b(?:globalThis\s*\.\s*)?fetch\s*\(", content, re.I | re.ASCII
            )
            market_calls = re.findall(
                r"\bfetch\s*\(\s*`/api/market/", content, re.I | re.ASCII
            )
            if len(calls) != len(market_calls):
                violations.append(f"{relative} 存在非平台 market proxy 的网络请求 API。")
            violations.extend(
                pattern_violations(relative, content, CATALOG["MOCK_ARTIFACT_PATTERNS"])
            )
    page = read(root / "src/App.vue")
    if page and not re.search(
        r"virtual:dashboard-data|data_file/final/dashboard-data\.json|/api/market",
        page,
    ):
        violations.append("src/App.vue 没有使用标准 final 数据文件或 /api/market 同源接口。")
    warnings = []
    try:
        package = json.loads(read(root / "package.json") or "{}")
        dependencies = [*package.get("dependencies", {}), *package.get("devDependencies", {})]
        warnings = [
            f"package.json 引入 {name}，生成看板优先使用平台内置 SVG/CSS 图表，避免额外依赖拖慢 build。"
            for name in dependencies
            if name in CATALOG["DISCOURAGED_VISUALIZATION_DEPENDENCIES"]
        ]
    except (ValueError, TypeError):
        pass
    if violations:
        return {
            "status": "failed",
            "summary": "生成产物未满足 AlphaDeck 硬约束。",
            "details": "\n".join(violations[:20]),
            "metadata": {
                "checkedFiles": len(files),
                "violationCount": len(violations),
                "warningCount": len(warnings),
            },
        }
    if warnings:
        return {
            "status": "warning",
            "summary": "生成产物满足硬约束，但存在可优化依赖。",
            "details": "\n".join(warnings[:10]),
            "metadata": {"checkedFiles": len(files), "warningCount": len(warnings)},
        }
    return {
        "status": "passed",
        "summary": "生成产物满足本地化、真实数据绑定和安全策略。",
        "metadata": {"checkedFiles": len(files)},
    }
