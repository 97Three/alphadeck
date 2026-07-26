import asyncio
import hashlib
import json
import math
import re
from copy import deepcopy

from .config import ROOT
from .file_tools import record, write_batch
from .messages import js_length
from .skill_integrity import read_regular
from .source_syntax import (
    client_directive,
    css_rules,
    declaration_identity,
    declaration_names,
    default_export,
    parse_source,
    statements,
    variable_count,
)
from .tool_paths import ToolError

CATALOG = json.loads((ROOT / "config/semantic-edit-catalog.json").read_text())


def parse_input(value, max_replacement=200000):
    value = record(value)

    def string(key, maximum, empty=False):
        item = value.get(key)
        if not isinstance(item, str) or not empty and not item:
            raise ToolError(
                "INVALID_TOOL_INPUT",
                key + " must be a" + ("" if empty else " non-empty") + " string.",
            )
        if js_length(item) > maximum:
            raise ToolError("INVALID_TOOL_INPUT", f"{key} exceeds {maximum} characters.")
        return item

    path, kind = string("path", 1024), string("kind", 64)
    before = string("beforeSha256", 64).lower()
    if not re.fullmatch("[a-f0-9]{64}", before):
        raise ToolError(
            "INVALID_TOOL_INPUT",
            "beforeSha256 must be a lowercase or uppercase 64-character SHA-256 digest.",
        )
    result = {
        "path": path,
        "kind": kind,
        "beforeSha256": before,
        "replacement": string("replacement", max_replacement, True),
    }
    if kind in {"typescript_symbol", "css_rule"}:
        key = "symbol" if kind == "typescript_symbol" else "selector"
        result[key] = string(key, 256 if key == "symbol" else 1024)
    elif kind == "line_range":
        if "startLine" not in value or "endLine" not in value:
            raise ToolError(
                "INVALID_TOOL_INPUT", "startLine and endLine are required for line_range edits."
            )
        for key in ("startLine", "endLine"):
            item = value[key]
            if type(item) not in {int, float} or not 1 <= item <= 1000000 or item != int(item):
                raise ToolError(
                    "INVALID_TOOL_INPUT", key + " must be an integer between 1 and 1000000."
                )
            result[key] = int(item)
        if result["endLine"] < result["startLine"]:
            raise ToolError(
                "INVALID_TOOL_INPUT", "endLine must be greater than or equal to startLine."
            )
    elif kind != "css_append":
        raise ToolError(
            "INVALID_TOOL_INPUT",
            "kind must be typescript_symbol, css_rule, css_append, or line_range.",
        )
    return result


def line_at(content, byte):
    return len(
        re.split(r"\r\n|\r|\n|\u2028|\u2029", content.encode()[:byte].decode("utf-8", "ignore"))
    )


def vue_script(content):
    opening = re.search(
        r'''<script\b(?=[^>]*\bsetup\b)(?=[^>]*\blang\s*=\s*["']ts["'])[^>]*>''',
        content,
        re.I,
    )
    if opening is None:
        raise ToolError(
            "SEMANTIC_SOURCE_PARSE_FAILED",
            "Vue source must contain one <script setup lang=\"ts\"> block for TypeScript symbol edits.",
        )
    closing = re.search(r"</script\s*>", content[opening.end():], re.I)
    if closing is None or re.search(r"<script\b", content[opening.end():], re.I):
        raise ToolError("SEMANTIC_SOURCE_PARSE_FAILED", "Vue source has an ambiguous script block.")
    start = opening.end()
    end = start + closing.start()
    return start, end, content[start:end]


def replace_typescript(path, content, symbol, replacement):
    is_vue = path.lower().endswith(".vue")
    if not is_vue and not re.search(r"\.tsx?$", path, re.I):
        raise ToolError(
            "SEMANTIC_EDIT_FILE_TYPE_MISMATCH",
            "typescript_symbol edits require a .ts, .tsx or Vue TypeScript file.",
        )
    script_start, _, script = vue_script(content) if is_vue else (0, len(content), content)
    parser_path = "script.ts" if is_vue else path
    source = parse_source(parser_path, script)
    if source.root_node.has_error:
        raise ToolError(
            "SEMANTIC_SOURCE_PARSE_FAILED",
            f"Cannot safely edit {path} because its TypeScript syntax is invalid.",
        )
    matches = [node for node in statements(source) if symbol in declaration_names(node)]
    if not matches:
        raise ToolError(
            "SEMANTIC_TARGET_NOT_FOUND",
            f"No top-level TypeScript declaration named {symbol} exists in {path}.",
        )
    if len(matches) > 1:
        raise ToolError(
            "SEMANTIC_TARGET_AMBIGUOUS",
            f"More than one top-level TypeScript declaration is named {symbol} in {path}. Do not retry this semantic target or reread the unchanged file; reuse beforeSha256 and the last query_text_file line numbers with kind=line_range.",
        )
    target = matches[0]
    if (
        declaration_identity(target, CATALOG["modifiers"])["declarationKind"] == "variable"
        and variable_count(target) > 1
    ):
        raise ToolError(
            "SEMANTIC_TARGET_UNSAFE",
            f"Declaration {symbol} shares a variable statement with other symbols and cannot be replaced safely.",
        )
    if not replacement.strip():
        raise ToolError(
            "SEMANTIC_REPLACEMENT_INVALID",
            "typescript_symbol replacement cannot be empty; use an explicit line_range edit to remove code.",
        )
    changed = parse_source(parser_path, replacement)
    nodes = statements(changed)
    if changed.root_node.has_error or len(nodes) != 1:
        raise ToolError(
            "SEMANTIC_REPLACEMENT_INVALID",
            "typescript_symbol replacement must be exactly one syntactically valid top-level statement.",
        )
    candidate = nodes[0]
    if variable_count(candidate) > 1:
        raise ToolError(
            "SEMANTIC_REPLACEMENT_IDENTITY_MISMATCH",
            f"Replacement for {symbol} must contain exactly one variable declarator.",
        )
    if declaration_names(candidate) != [symbol]:
        raise ToolError(
            "SEMANTIC_REPLACEMENT_IDENTITY_MISMATCH",
            f"Replacement must contain exactly one declaration for the target symbol {symbol}.",
        )
    expected, received = (
        declaration_identity(target, CATALOG["modifiers"]),
        declaration_identity(candidate, CATALOG["modifiers"]),
    )
    if expected != received:
        raise ToolError(
            "SEMANTIC_REPLACEMENT_IDENTITY_MISMATCH",
            f"Replacement for {symbol} must preserve its declaration kind, modifiers, async/generator status, and variable declaration kind.",
            {"expected": expected, "received": received},
        )
    encoded = content.encode()
    offset = len(content[:script_start].encode())
    return {
        "content": (
            encoded[: offset + target.start_byte] + replacement.encode() + encoded[offset + target.end_byte :]
        ).decode(),
        "startLine": line_at(content, offset + target.start_byte),
        "endLine": line_at(content, offset + max(target.start_byte, target.end_byte - 1)),
    }


def replace_css(path, content, selector, replacement):
    if not path.lower().endswith(".css"):
        raise ToolError(
            "SEMANTIC_EDIT_FILE_TYPE_MISMATCH", "css_rule edits require a .css stylesheet."
        )
    source = parse_source(path, content)
    if source.root_node.has_error:
        raise ToolError(
            "SEMANTIC_SOURCE_PARSE_FAILED",
            f"Cannot safely edit {path} because its CSS syntax is invalid.",
        )
    matches = [node for node, name in css_rules(source) if name == selector.strip()]
    if not matches:
        raise ToolError(
            "SEMANTIC_TARGET_NOT_FOUND",
            f"CSS selector {selector} was not found in {path}. Query the exact existing selector; for a bounded multi-rule visual override, reuse beforeSha256 with kind=css_append instead of inventing a combined selector.",
        )
    if len(matches) > 1:
        raise ToolError(
            "SEMANTIC_TARGET_AMBIGUOUS",
            f"CSS selector {selector} occurs more than once in {path}. Do not retry this semantic target or reread the unchanged file; reuse beforeSha256 and the last query_text_file line numbers with kind=line_range.",
        )
    changed = parse_source(path, replacement)
    if changed.root_node.has_error:
        raise ToolError(
            "SEMANTIC_REPLACEMENT_INVALID",
            "css_rule replacement must contain one syntactically valid rule.",
        )
    nodes = changed.root_node.named_children
    if len(nodes) != 1 or nodes[0].type != "rule_set":
        raise ToolError(
            "SEMANTIC_REPLACEMENT_INVALID",
            "css_rule replacement must contain exactly one CSS rule.",
        )
    if css_rules(changed)[0][1] != selector.strip():
        raise ToolError(
            "SEMANTIC_REPLACEMENT_IDENTITY_MISMATCH",
            f"Replacement selector must remain {selector}.",
        )
    target, encoded = matches[0], content.encode()
    return {
        "content": (
            encoded[: target.start_byte] + replacement.encode() + encoded[target.end_byte :]
        ).decode(),
        "startLine": target.start_point.row + 1,
        "endLine": line_at(content, target.end_byte - 1),
    }


def line_count(content):
    return len(re.split(r"\r?\n", content)) - int(content.endswith("\n"))


def replace_lines(content, start, end, replacement):
    newline = "\r\n" if "\r\n" in content else "\n"
    trailing = content.endswith("\n")
    lines = re.split(r"\r?\n", content)
    if trailing:
        lines.pop()
    if start > len(lines) or end > len(lines):
        raise ToolError(
            "SEMANTIC_TARGET_NOT_FOUND",
            f"Requested line range {start}-{end} exceeds the {len(lines)}-line file.",
        )
    changed = newline.join(
        [
            *lines[: start - 1],
            *(re.split(r"\r?\n", replacement) if replacement else []),
            *lines[end:],
        ]
    )
    return {"content": changed + (newline if trailing else ""), "startLine": start, "endLine": end}


def append_css(path, content, replacement):
    if not path.lower().endswith(".css"):
        raise ToolError(
            "SEMANTIC_EDIT_FILE_TYPE_MISMATCH", "css_append edits require a .css stylesheet."
        )
    normalized = replacement.strip()
    if not normalized:
        raise ToolError(
            "SEMANTIC_REPLACEMENT_INVALID",
            "css_append replacement cannot be empty or whitespace-only.",
        )
    count = line_count(normalized)
    if js_length(normalized) > 16000 or count > 160:
        raise ToolError(
            "SEMANTIC_TARGET_UNSAFE",
            "css_append is limited to 16000 characters and 160 lines; append only the minimal override rules.",
            {
                "replacementChars": js_length(normalized),
                "replacementLines": count,
                "maxChars": 16000,
                "maxLines": 160,
            },
        )
    if parse_source(path, normalized).root_node.has_error:
        raise ToolError(
            "SEMANTIC_REPLACEMENT_INVALID",
            "css_append replacement must contain syntactically valid CSS.",
        )
    newline = "\r\n" if "\r\n" in content else "\n"
    existing = content.rstrip()
    start = line_count(existing) + 2
    return {
        "content": existing + newline + newline + normalized + newline,
        "startLine": start,
        "endLine": start + count - 1,
    }


def validate_edited(path, before, changed):
    if before == changed:
        raise ToolError("SEMANTIC_NO_CHANGE", f"The semantic edit does not change {path}.")
    if path.lower().endswith(".vue"):
        _, _, original_script = vue_script(before)
        _, _, edited_script = vue_script(changed)
        if parse_source("script.ts", edited_script).root_node.has_error:
            raise ToolError("SEMANTIC_REPLACEMENT_INVALID", f"The semantic edit would leave {path} with invalid TypeScript syntax.")
        if before.count("<template") != changed.count("<template") or before.count("</template>") != changed.count("</template>"):
            raise ToolError("SEMANTIC_REPLACEMENT_INVALID", "The semantic edit must preserve the Vue template block.")
        return
    source, edited = parse_source(path, before), parse_source(path, changed)
    if edited.root_node.has_error:
        raise ToolError(
            "SEMANTIC_REPLACEMENT_INVALID",
            f"The semantic edit would leave {path} with invalid {'CSS' if path.lower().endswith('.css') else 'TypeScript'} syntax.",
        )
    if path.lower().endswith(".css"):
        if not changed.strip():
            raise ToolError(
                "SEMANTIC_REPLACEMENT_INVALID", f"The semantic edit cannot empty {path}."
            )
    else:
        if default_export(source) and not default_export(edited):
            raise ToolError(
                "SEMANTIC_REPLACEMENT_IDENTITY_MISMATCH",
                f"The semantic edit must preserve the default export in {path}.",
            )
        if client_directive(source) and not client_directive(edited):
            raise ToolError(
                "SEMANTIC_REPLACEMENT_IDENTITY_MISMATCH",
                f"The semantic edit must preserve the use client directive in {path}.",
            )


def apply_edit(input, content):
    path, kind, replacement = input["path"], input["kind"], input["replacement"]
    if not re.search(r"\.(?:ts|tsx|css|vue)$", path, re.I):
        raise ToolError(
            "SEMANTIC_EDIT_FILE_TYPE_MISMATCH",
            "semantic_edit only supports existing .ts, .tsx, .vue, and .css source files.",
        )
    if kind == "typescript_symbol":
        changed, target = (
            replace_typescript(path, content, input["symbol"], replacement),
            input["symbol"],
        )
    elif kind == "css_rule":
        changed, target = (
            replace_css(path, content, input["selector"], replacement),
            input["selector"],
        )
    elif kind == "css_append":
        changed, target = append_css(path, content, replacement), "append"
    else:
        start, end, total = input["startLine"], input["endLine"], line_count(content)
        if not replacement.strip():
            raise ToolError(
                "SEMANTIC_REPLACEMENT_INVALID",
                "line_range replacement cannot be empty or whitespace-only.",
            )
        limit = max(1, total - 1) if total <= 20 else min(80, max(1, math.ceil(total * 0.35)))
        affected = end - start + 1
        if affected > limit:
            raise ToolError(
                "SEMANTIC_TARGET_UNSAFE",
                f"line_range may replace at most {limit} of {total} lines; use a named TypeScript declaration or CSS rule, or kind=css_append for a bounded multi-rule stylesheet override.",
                {"affectedLines": affected, "allowedLines": limit, "totalLines": total},
            )
        changed, target = replace_lines(content, start, end, replacement), f"{start}-{end}"
    validate_edited(path, content, changed["content"])
    return {**changed, "target": target}


class SemanticEditTool:
    def __init__(
        self,
        policy,
        *,
        timeout_ms=15000,
        max_file_bytes=1000000,
        max_write_bytes=None,
        max_replacement_chars=200000,
    ):
        self.policy, self.timeout, self.maximum, self.write, self.replacement = (
            policy,
            timeout_ms,
            max_file_bytes,
            max_write_bytes or max_file_bytes,
            max_replacement_chars,
        )
        self.definition = deepcopy(CATALOG["definition"])

    async def execute(self, value, context):
        try:
            async with asyncio.timeout(self.timeout / 1000):
                input = parse_input(value, self.replacement)
                self.policy.write(input["path"])
                resolved = self.policy.read(input["path"])
                if not resolved.canonical.is_file():
                    raise ToolError("NOT_A_FILE", f"Expected a file: {resolved.relative}.")
                if resolved.canonical.stat().st_size > self.maximum:
                    raise ToolError(
                        "FILE_TOO_LARGE",
                        f"{resolved.relative} exceeds the {self.maximum}-byte semantic edit limit.",
                    )
                content = read_regular(resolved.canonical, self.maximum)
                if b"\x00" in content:
                    raise ToolError(
                        "BINARY_FILE_DENIED", f"Cannot edit binary file {resolved.relative}."
                    )
                before = hashlib.sha256(content).hexdigest()
                if before != input["beforeSha256"]:
                    raise ToolError(
                        "WORKSPACE_WRITE_CONFLICT",
                        f"The target changed after it was read: {resolved.relative}. Query it again before editing.",
                        {"expectedSha256": input["beforeSha256"], "actualSha256": before},
                    )
                edited = apply_edit(input, content.decode("utf-8", "replace"))
                result = await write_batch(
                    self.policy,
                    [
                        {
                            "path": input["path"],
                            "content": edited["content"].encode(),
                            "expectedBeforeSha256": before,
                        }
                    ],
                    context,
                    max_file=self.write,
                    max_total=self.write,
                )
                file = result["files"][0]
                data = {
                    "path": file["path"],
                    "kind": input["kind"],
                    "target": edited["target"],
                    "startLine": edited["startLine"],
                    "endLine": edited["endLine"],
                    "beforeSha256": before,
                    "afterSha256": file["afterSha256"],
                    "bytes": file["bytes"],
                }
                return {
                    "ok": True,
                    "data": data,
                    "content": f"Semantically edited {file['path']} ({input['kind']} {edited['target']}, lines {edited['startLine']}-{edited['endLine']}).",
                }
        except TimeoutError:
            error = ToolError("TOOL_TIMEOUT", f"PI Agent tool timed out after {self.timeout}ms.")
        except ToolError as cause:
            error = cause
        except Exception as cause:
            error = ToolError("TOOL_EXECUTION_FAILED", str(cause))
        return {
            "ok": False,
            "error": {
                "code": error.code,
                "message": error.message,
                **({"details": error.details} if error.details is not None else {}),
            },
            "content": error.message,
        }
