import base64
import json
import re
import stat
from dataclasses import dataclass

from .config import ROOT
from .context_capsule import collect_targets
from .messages import js_length, js_slice
from .mutation_journal import MutationConflict, MutationJournal, current_hash, sha
from .resource_lock import resource_lock
from .skill_integrity import read_regular
from .tool_paths import ToolError, WorkspacePolicy, matches_glob

UNSET = object()
IGNORED = {".git", ".pi-mutation-journal", ".pi-workspace.lock", ".next", "node_modules"}


def record(value):
    if not isinstance(value, dict):
        raise ToolError("INVALID_TOOL_INPUT", "Tool input must be a JSON object.")
    return value


def string(body, key, default=UNSET, *, empty=False, maximum=None):
    value = body.get(key, default)
    if (
        not isinstance(value, str)
        or (not empty and not value)
        or (maximum is not None and js_length(value) > maximum)
    ):
        raise ToolError("INVALID_TOOL_INPUT", key + " must be a bounded string.")
    return value


def integer(body, key, default, minimum, maximum):
    value = body.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ToolError("INVALID_TOOL_INPUT", key + " must be an integer in its supported range.")
    return value


def boolean(body, key, default):
    value = body.get(key, default)
    if type(value) is not bool:
        raise ToolError("INVALID_TOOL_INPUT", key + " must be a boolean.")
    return value


def truncate(value, limit=12000):
    size = js_length(value)
    if size <= limit:
        return {"text": value, "truncated": False, "originalChars": size}
    if limit == 0:
        return {"text": "", "truncated": True, "originalChars": size}

    def marker(omitted):
        return f"\n\n[PI Agent output truncated: omitted {omitted} characters from {size}.]\n\n"

    note = marker(size)
    preserved = max(0, limit - js_length(note))
    for _ in range(4):
        note = marker(size - preserved)
        next_preserved = max(0, limit - js_length(note))
        if next_preserved == preserved:
            break
        preserved = next_preserved
    if js_length(note) >= limit:
        text = js_slice(note, 0, limit)
    else:
        tail = min(preserved // 3, 4000)
        text = (
            js_slice(value, 0, preserved - tail) + note + (js_slice(value, -tail) if tail else "")
        )
    return {"text": text, "truncated": True, "originalChars": size}


@dataclass
class ToolContext:
    run_id: str
    operation_id: str
    commit_mutation: object = None


async def write_batch(policy, files, context, *, max_file=1000000, max_total=1000000, lock_wait=5):
    if context is None or context.commit_mutation is None:
        raise ToolError(
            "WORKSPACE_COMMIT_FENCE_REQUIRED",
            "Workspace writes require a durable mutation commit fence.",
        )
    if not 1 <= len(files) <= 8:
        raise ToolError("INVALID_BATCH_WRITE", "A batch must contain between 1 and 8 files.")
    if (
        any(len(item["content"]) > max_file for item in files)
        or sum(len(item["content"]) for item in files) > max_total
    ):
        raise ToolError("WRITE_TOO_LARGE", "Workspace write exceeds its byte budget.")
    async with resource_lock(
        policy.root,
        {
            "purpose": "workspace_write",
            "runId": context.run_id,
            "operationId": context.operation_id,
        },
        wait_seconds=lock_wait,
        recover_dead_local_owner=True,
    ):
        staged = []
        targets = set()
        journal = None
        try:
            for item in files:
                path = policy.write(item["path"])
                path.canonical.parent.mkdir(parents=True, exist_ok=True)
                path = policy.write(item["path"])
                if path.canonical in targets:
                    raise ToolError(
                        "DUPLICATE_BATCH_TARGET",
                        "A batch cannot target the same canonical file twice.",
                    )
                targets.add(path.canonical)
                before = current_hash(policy.root, path.canonical_relative)
                if "expectedBeforeSha256" in item and item["expectedBeforeSha256"] != before:
                    raise ToolError(
                        "WORKSPACE_WRITE_CONFLICT", "Target content changed before staging."
                    )
                mode = stat.S_IMODE(path.canonical.stat().st_mode) if path.exists else 0o644
                staged.append(
                    {
                        "target": path.canonical_relative,
                        "relative": path.relative,
                        "content": item["content"],
                        "mode": mode,
                        "existedBefore": path.exists,
                        "beforeSha256": before,
                        "afterSha256": sha(item["content"]),
                    }
                )
            try:
                journal = MutationJournal.prepare(
                    policy.root, context.run_id, context.operation_id, staged
                )
            except MutationConflict as error:
                raise ToolError(
                    "WORKSPACE_WRITE_CONFLICT", "Target changed before durable staging."
                ) from error
            except Exception as error:
                raise ToolError(
                    "WORKSPACE_JOURNAL_PREPARE_FAILED",
                    "Cannot durably prepare the workspace mutation journal.",
                ) from error

            async def commit():
                for item in staged:
                    resolved = policy.write(item["relative"])
                    if (
                        resolved.canonical_relative != item["target"]
                        or resolved.exists != item["existedBefore"]
                        or current_hash(policy.root, item["target"]) != item["beforeSha256"]
                    ):
                        raise ToolError(
                            "WORKSPACE_WRITE_CONFLICT",
                            "Target identity or content changed before commit.",
                        )
                journal.commit()

            await context.commit_mutation(commit)
        except BaseException as error:
            if journal:
                started = journal.manifest["state"] != "prepared"
                journal.rollback()
                if started:
                    raise ToolError(
                        "WORKSPACE_MUTATION_ROLLED_BACK",
                        "Workspace mutation failed and its durable journal restored every target.",
                    ) from error
            raise
        return {
            "totalBytes": sum(len(item["content"]) for item in staged),
            "files": [
                {
                    "path": item["relative"],
                    "bytes": len(item["content"]),
                    "created": not item["existedBefore"],
                    "beforeSha256": item["beforeSha256"],
                    "afterSha256": item["afterSha256"],
                }
                for item in staged
            ],
        }


class FileTools:
    def __init__(
        self,
        root,
        *,
        allowed_write_globs=(),
        include_default=True,
        structured_json_globs=(),
        max_file=1000000,
        max_write=1000000,
        max_output=12000,
    ):
        self.policy = WorkspacePolicy(root, allowed_write_globs, include_default=include_default)
        self.structured = tuple(structured_json_globs)
        self.max_file, self.max_write, self.max_output = max_file, max_write, max_output
        self.definitions = json.loads((ROOT / "config/python-tool-catalog.json").read_text())[
            "tools"
        ]

    def project_context_receipt(self, name, arguments, result):
        if name not in {
            "list_files",
            "read_file",
            "read_file_range",
            "search_files",
            "write_file",
            "edit_file",
            "apply_patch",
            "submit_result",
            "query_json",
            "query_text_file",
        }:
            return None
        return collect_targets(arguments, result.get("data"))

    def read_text(self, path, *, raw=True):
        resolved = self.policy.read(path)
        if not resolved.canonical.is_file():
            raise ToolError("NOT_A_FILE", "Expected a file.")
        if resolved.canonical.stat().st_size > self.max_file:
            raise ToolError("FILE_TOO_LARGE", "File exceeds the tool byte budget.")
        data = read_regular(resolved.canonical, self.max_file)
        if b"\0" in data:
            raise ToolError("BINARY_FILE_DENIED", "Cannot read a binary file as text.")
        content = data.decode(errors="replace")
        if raw and (
            resolved.relative.lower().endswith(".json")
            or resolved.canonical_relative.lower().endswith(".json")
        ):
            try:
                json.loads(content)
                valid = True
            except ValueError:
                valid = False
            structured = any(
                matches_glob(resolved.relative, glob)
                or matches_glob(resolved.canonical_relative, glob)
                for glob in self.structured
            )
            if valid and (structured or js_length(content) > self.max_output):
                raise ToolError(
                    "STRUCTURED_JSON_QUERY_REQUIRED",
                    "Use one query_json call with all required pointers for structured JSON.",
                )
        return resolved, content, data

    def listing(self, path=".", recursive=True, max_depth=4, max_entries=300, *, ignore=False):
        start = self.policy.read(path, allow_root=True)
        if not start.canonical.is_dir():
            return {
                "entries": [
                    {
                        "path": start.relative,
                        "type": "file",
                        "bytes": start.canonical.stat().st_size,
                    }
                ],
                "truncated": False,
                "skippedUnsafeLinks": 0,
                "skippedSensitivePaths": 0,
            }
        result = {
            "entries": [],
            "truncated": False,
            "skippedUnsafeLinks": 0,
            "skippedSensitivePaths": 0,
        }
        visited = set()

        def visit(logical, directory, depth):
            if len(result["entries"]) >= max_entries:
                result["truncated"] = True
                return
            if directory in visited:
                return
            visited.add(directory)
            for entry in sorted(
                directory.iterdir(), key=lambda path: (path.name.lower(), path.name)
            ):
                if len(result["entries"]) >= max_entries:
                    result["truncated"] = True
                    return
                if ignore and entry.is_dir() and not entry.is_symlink() and entry.name in IGNORED:
                    continue
                relative = entry.name if logical == "." else logical + "/" + entry.name
                try:
                    resolved = self.policy.read(relative)
                except ToolError as error:
                    if entry.is_symlink() and error.code == "SYMLINK_ESCAPE_DENIED":
                        result["skippedUnsafeLinks"] += 1
                        continue
                    if error.code == "SENSITIVE_READ_PATH_DENIED":
                        result["skippedSensitivePaths"] += 1
                        continue
                    raise
                kind = (
                    "symlink"
                    if entry.is_symlink()
                    else "directory"
                    if resolved.canonical.is_dir()
                    else "file"
                )
                result["entries"].append(
                    {
                        "path": relative,
                        "type": kind,
                        **(
                            {"bytes": resolved.canonical.stat().st_size}
                            if resolved.canonical.is_file()
                            else {}
                        ),
                    }
                )
                if (
                    recursive
                    and resolved.canonical.is_dir()
                    and depth < max_depth
                    and not (ignore and entry.name in IGNORED)
                ):
                    visit(relative, resolved.canonical, depth + 1)

        visit(start.relative, start.canonical, 0)
        return result

    async def execute(self, name, input, context=None):
        try:
            return await self._execute(name, record(input), context)
        except ToolError as error:
            return {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    **({"details": error.details} if error.details is not None else {}),
                },
                "content": error.message,
            }

    async def _execute(self, name, body, context):
        if name == "list_files":
            result = self.listing(
                string(body, "path", ".", maximum=1024),
                boolean(body, "recursive", True),
                integer(body, "maxDepth", 4, 0, 12),
                integer(body, "maxEntries", 300, 1, 1000),
            )
            rendered = "\n".join(
                ("dir" if item["type"] == "directory" else item["type"])
                + (f" {item['bytes']}b" if "bytes" in item else "")
                + "\t"
                + item["path"]
                for item in result["entries"]
            )
            output = truncate(rendered, self.max_output)
            return {
                "ok": True,
                "data": {
                    "entryCount": len(result["entries"]),
                    "truncated": result["truncated"] or output["truncated"],
                    "skippedUnsafeLinks": result["skippedUnsafeLinks"],
                    "skippedSensitivePaths": result["skippedSensitivePaths"],
                },
                "content": output["text"],
            }
        if name in {"read_file", "read_file_range"}:
            path = string(body, "path", maximum=1024)
            resolved, content, data = self.read_text(path)
            if name == "read_file":
                output = truncate(content, self.max_output)
                details = {
                    "path": resolved.relative,
                    "bytes": len(data),
                    "truncated": output["truncated"],
                    "originalChars": output["originalChars"],
                }
            else:
                start = integer(body, "startLine", 1, 1, 1000000)
                end = integer(body, "endLine", start + 199, start, 1000000)
                if end - start + 1 > 500:
                    raise ToolError(
                        "INVALID_TOOL_INPUT", "read_file_range accepts at most 500 lines per call."
                    )
                lines = re.split(r"\r?\n", content)
                rendered = "\n".join(
                    f"{start + index}: {line}" for index, line in enumerate(lines[start - 1 : end])
                )
                output = truncate(rendered, self.max_output)
                details = {
                    "path": resolved.relative,
                    "startLine": start,
                    "endLine": min(end, len(lines)),
                    "totalLines": len(lines),
                    "truncated": output["truncated"],
                }
            return {"ok": True, "data": details, "content": output["text"]}
        if name == "search_files":
            query = string(body, "query", maximum=2000)
            sensitive = boolean(body, "caseSensitive", False)
            maximum = integer(body, "maxResults", 100, 1, 500)
            glob = string(body, "fileGlob", maximum=256) if "fileGlob" in body else None
            listing = self.listing(
                string(body, "path", ".", maximum=1024), True, 12, 5000, ignore=True
            )
            needle = query if sensitive else query.lower()
            found = []
            skipped = 0
            for entry in listing["entries"]:
                if entry["type"] != "file" or (glob and not matches_glob(entry["path"], glob)):
                    continue
                if entry.get("bytes", 0) > self.max_file:
                    skipped += 1
                    continue
                try:
                    _, content, _ = self.read_text(entry["path"], raw=False)
                except ToolError as error:
                    if error.code == "BINARY_FILE_DENIED":
                        continue
                    raise
                for index, line in enumerate(re.split(r"\r?\n", content)):
                    haystack = line if sensitive else line.lower()
                    column = haystack.find(needle)
                    if column >= 0:
                        found.append(
                            f"{entry['path']}:{index + 1}:{js_length(line[:column]) + 1}: {js_slice(line, 0, 500)}"
                        )
                    if len(found) >= maximum:
                        break
                if len(found) >= maximum:
                    break
            output = truncate("\n".join(found), self.max_output)
            return {
                "ok": True,
                "data": {
                    "matchCount": len(found),
                    "truncated": len(found) >= maximum
                    or listing["truncated"]
                    or output["truncated"],
                    "skippedLargeFiles": skipped,
                    "skippedUnsafeLinks": listing["skippedUnsafeLinks"],
                    "skippedSensitivePaths": listing["skippedSensitivePaths"],
                },
                "content": output["text"] or "No matches.",
            }
        if name in {"write_file", "edit_file", "apply_patch"}:
            path = string(body, "path", maximum=1024)
            expected = UNSET
            if name == "write_file":
                content = string(body, "content", empty=True)
                encoding = string(body, "encoding", "utf8")
                if encoding not in {"utf8", "base64"}:
                    raise ToolError("INVALID_TOOL_INPUT", "encoding must be utf8 or base64.")
                if encoding == "base64":
                    cleaned = (
                        re.sub(r"[^A-Za-z0-9+/=_-]", "", content)
                        .replace("-", "+")
                        .replace("_", "/")
                        .split("=", 1)[0]
                    )
                    cleaned = cleaned[: len(cleaned) - 1] if len(cleaned) % 4 == 1 else cleaned
                    encoded = base64.b64decode(cleaned + "=" * ((-len(cleaned)) % 4))
                else:
                    encoded = content.encode()
                replacements = 0
            else:
                self.policy.write(path)
                _, content, before = self.read_text(path, raw=False)
                expected = sha(before)
                edits = (
                    [
                        {
                            "oldText": string(body, "oldText"),
                            "newText": string(body, "newText", empty=True),
                        }
                    ]
                    if name == "edit_file"
                    else body.get("edits")
                )
                if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
                    raise ToolError(
                        "INVALID_TOOL_INPUT", "edits must contain between 1 and 50 replacements."
                    )
                for edit in edits:
                    edit = record(edit)
                    old, new = string(edit, "oldText"), string(edit, "newText", empty=True)
                    first = content.find(old)
                    if first < 0:
                        raise ToolError(
                            "EDIT_MATCH_NOT_FOUND", "oldText was not found in the target file."
                        )
                    if content.find(old, first + 1) >= 0:
                        raise ToolError(
                            "EDIT_MATCH_AMBIGUOUS",
                            "oldText occurs more than once; provide a unique match.",
                        )
                    content = content[:first] + new + content[first + len(old) :]
                encoded = content.encode()
                replacements = len(edits)
            file = {
                "path": path,
                "content": encoded,
                **({"expectedBeforeSha256": expected} if expected is not UNSET else {}),
            }
            result = (
                await write_batch(
                    self.policy, [file], context, max_file=self.max_write, max_total=self.max_write
                )
            )["files"][0]
            if name == "write_file":
                message = (
                    "Created" if result["created"] else "Updated"
                ) + f" {result['path']} ({result['bytes']} bytes)."
            else:
                result["replacements"] = replacements
                message = (
                    f"Edited {result['path']} (1 replacement)."
                    if name == "edit_file"
                    else f"Patched {result['path']} ({replacements} replacements)."
                )
            return {"ok": True, "data": result, "content": message}
        if name == "submit_result":
            summary = string(body, "summary", maximum=8000)
            artifacts = body.get("artifacts", [])
            if (
                not isinstance(artifacts, list)
                or len(artifacts) > 50
                or any(
                    not isinstance(path, str) or not 1 <= js_length(path) <= 1024
                    for path in artifacts
                )
            ):
                raise ToolError(
                    "INVALID_TOOL_INPUT",
                    "artifacts must contain at most 50 workspace-relative paths.",
                )
            artifacts = list(dict.fromkeys(artifacts))
            verified = []
            for path in artifacts:
                resolved = self.policy.read(path)
                if not resolved.canonical.is_file():
                    raise ToolError("NOT_A_FILE", "Submitted artifact is not a file.")
                verified.append(resolved.relative)
            data = {
                "summary": summary,
                "artifacts": artifacts,
                **(
                    {"notes": string(body, "notes", empty=True, maximum=8000)}
                    if "notes" in body
                    else {}
                ),
                "candidateStatus": "candidate_complete",
                "verifiedArtifacts": verified,
            }
            return {
                "ok": True,
                "data": data,
                "content": "Candidate submitted for platform verification: "
                + summary
                + ("\nArtifacts: " + ", ".join(verified) if verified else ""),
            }
        raise ToolError("UNKNOWN_TOOL", "Unknown Agent tool: " + name)
