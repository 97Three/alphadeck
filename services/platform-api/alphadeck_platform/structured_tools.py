import json
import math
import re

from .config import ROOT
from .file_tools import FileTools, integer, string, truncate
from .messages import js_length, js_slice
from .mutation_journal import sha
from .provenance import js_json, object_keys
from .tool_paths import ToolError

PATHS = {
    "final_result": "data_file/final/result.json",
    "sources_evidence": "evidence/sources.json",
    "data_quality_evidence": "evidence/data_quality.json",
    "task": ".data-agent/task.json",
    "plan": ".data-agent/plan.json",
    "validation_report": ".data-agent/validation.json",
}
PREFERRED = ["id", "name", "summary", "status", "rows", "data", "quality", "createdAt", "updatedAt"]
MISSING = object()


def pointer_segments(pointer):
    if not pointer:
        return []
    if not pointer.startswith("/") or re.search(r"~(?:[^01]|$)", pointer):
        raise ToolError("INVALID_TOOL_INPUT", "Invalid RFC 6901 JSON Pointer.")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def resolve_pointer(root, pointer):
    current = root
    for part in pointer_segments(pointer):
        if isinstance(current, list):
            if not re.fullmatch(r"0|[1-9]\d*", part) or len(part) > 16 or int(part) >= len(current):
                return MISSING
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def value_type(value):
    if value is MISSING:
        return None
    return (
        "null"
        if value is None
        else "boolean"
        if type(value) is bool
        else "number"
        if type(value) in {int, float}
        else "string"
        if isinstance(value, str)
        else "array"
        if isinstance(value, list)
        else "object"
    )


def shape(value, compact=False):
    if isinstance(value, list):
        sample = next((item for item in value if isinstance(item, dict)), None)
        return {
            "originalSize": len(value),
            **(
                {"sampleItemKeys": object_keys(sample)[:48]}
                if not compact and sample is not None
                else {}
            ),
        }
    if isinstance(value, str):
        return {"originalSize": js_length(value)}
    if isinstance(value, dict) and not compact:
        return {"availableKeys": object_keys(value)[:64]}
    return {}


def selected_indexes(size, limit):
    if size <= limit:
        return list(range(size))
    if limit == 1:
        return [size - 1]
    head = max(1, limit // 3)
    return list(range(head)) + list(range(size - (limit - head), size))


def build_report(file, root, pointers, settings, omission_limit, compact, preferred):
    omissions = []
    count = 0

    def omit(pointer, kind, original, retained, **extras):
        nonlocal count
        count += 1
        if len(omissions) < omission_limit:
            omissions.append(
                {
                    "pointer": pointer,
                    "kind": kind,
                    "originalSize": original,
                    "retainedSize": retained,
                    **extras,
                }
            )

    def child_pointer(parent, segment):
        return parent + "/" + str(segment).replace("~", "~0").replace("/", "~1")

    def project(value, pointer, depth):
        if isinstance(value, str):
            size = js_length(value)
            limit = settings["maxStringChars"]
            if size <= limit:
                return value
            head = max(1, limit // 3)
            tail = max(0, limit - head - 1)
            projected = js_slice(value, 0, head) + "…" + (js_slice(value, -tail) if tail else "")
            omit(pointer, "string_chars", size, js_length(projected))
            return projected
        if not isinstance(value, (dict, list)):
            return value
        if depth >= settings["maxDepth"]:
            omit(pointer, "max_depth", len(value), 0)
            return {
                "$piAgent": "max_depth_summary",
                "valueType": "array" if isinstance(value, list) else "object",
                "size": len(value),
            }
        if isinstance(value, list):
            indexes = selected_indexes(len(value), settings["maxArrayItems"])
            if len(indexes) < len(value):
                omit(
                    pointer,
                    "array_items",
                    len(value),
                    len(indexes),
                    strategy="head_and_recent_tail",
                )
            return [
                project(value[index], child_pointer(pointer, index), depth + 1) for index in indexes
            ]
        keys = object_keys(value)
        if len(keys) > settings["maxObjectKeys"]:
            chosen = set()
            for key in preferred:
                if key in value:
                    chosen.add(keys.index(key))
                if len(chosen) >= settings["maxObjectKeys"]:
                    break
            if len(chosen) < settings["maxObjectKeys"]:
                remaining = [index for index in range(len(keys)) if index not in chosen]
                for index in selected_indexes(
                    len(remaining), settings["maxObjectKeys"] - len(chosen)
                ):
                    chosen.add(remaining[index])
            retained = [keys[index] for index in sorted(chosen)]
            omit(pointer, "object_keys", len(keys), len(retained))
        else:
            retained = keys
        return {
            key: project(value[key], child_pointer(pointer, key), depth + 1) for key in retained
        }

    queries = []
    for pointer in pointers:
        value = resolve_pointer(root, pointer)
        if value is MISSING:
            queries.append({"pointer": pointer, "found": False, "valueType": None})
        else:
            queries.append(
                {
                    "pointer": pointer,
                    "found": True,
                    "valueType": value_type(value),
                    **shape(value, compact),
                    "value": project(value, pointer, 0),
                }
            )
    report = {
        "$piAgent": {
            "kind": "bounded_json_pointer_query",
            "version": 1,
            "path": file["path"],
            "bytes": file["bytes"],
            "sha256": file["sha256"],
            "selection": "head_and_recent_tail",
            "omissionCount": count,
            "omissions": omissions,
            "omissionDetailsTruncated": count > len(omissions),
            **({"pathCorrection": file["pathCorrection"]} if "pathCorrection" in file else {}),
        },
        "queries": queries,
    }
    return report


def bounded_json(file, root, pointers, settings, maximum, preferred):
    current = dict(settings)
    detail_limit = min(16, max(2, 24 // len(pointers)))
    for _ in range(10):
        for omission_limit, compact in ((detail_limit, False), (0, False), (0, True)):
            report = build_report(file, root, pointers, current, omission_limit, compact, preferred)
            content = js_json(report, indent=2)
            if js_length(content) <= maximum:
                return content, any(current[key] < settings[key] for key in current) or report[
                    "$piAgent"
                ]["omissionCount"] > 0
        current = {
            "maxArrayItems": max(1, current["maxArrayItems"] // 2),
            "maxStringChars": max(24, current["maxStringChars"] // 2),
            "maxObjectKeys": max(4, current["maxObjectKeys"] // 2),
            "maxDepth": max(1, current["maxDepth"] - 1),
        }
    report = {
        "$piAgent": {
            "kind": "bounded_json_pointer_query",
            "version": 1,
            "path": file["path"],
            "bytes": file["bytes"],
            "sha256": file["sha256"],
            "valuesOmittedForOutputBudget": True,
            **({"pathCorrection": file["pathCorrection"]} if "pathCorrection" in file else {}),
            "retry": {
                "action": "split_pointer_batch",
                "suggestedPointersPerCall": "6-8",
                "message": "Retry with exact nested or leaf pointers; split into at most two related batches only if necessary.",
            },
        },
        "queries": [
            {
                "pointer": pointer,
                "found": (value := resolve_pointer(root, pointer)) is not MISSING,
                "valueType": value_type(value),
                **shape(value),
            }
            for pointer in pointers
        ],
    }
    content = js_json(report, indent=2)
    if js_length(content) > maximum:
        raise ToolError(
            "TOOL_OUTPUT_TOO_LARGE",
            "query_json metadata exceeds the output budget; request fewer pointers.",
        )
    return content, True


def tolerant_int(body, keys, default, minimum, maximum):
    value = next((body[key] for key in keys if key in body), default)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        value = int(value)
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or int(value) != value
        or abs(value) > 9007199254740991
    ):
        return default
    return max(minimum, min(maximum, int(value)))


class StructuredFileTools(FileTools):
    def __init__(self, *args, finance=False, json_artifacts=None, **options):
        super().__init__(*args, **options)
        catalog = json.loads((ROOT / "config/python-tool-catalog.json").read_text())
        self.finance = finance
        config = json_artifacts or (
            catalog["financeJson"]["configuration"] if finance else {"paths": PATHS}
        )
        self.artifacts = config["paths"]
        self.preferred = config.get("preferredObjectKeys", PREFERRED)
        self.alias_resolver = (
            config.get("resolveAlias") if callable(config.get("resolveAlias")) else None
        )
        self.alias_validator = (
            config.get("validateAliasIdentity")
            if callable(config.get("validateAliasIdentity"))
            else None
        )
        self.definitions.extend(catalog["structuredTools"])
        if finance:
            self.definitions = [
                catalog["financeJson"]["definition"] if item["name"] == "query_json" else item
                for item in self.definitions
            ]

    def json_reference(self, body):
        artifact = body.get("artifact")
        explicit = string(body, "path", maximum=1024) if "path" in body else None
        if "artifact" in body:
            if not isinstance(artifact, str) or artifact not in self.artifacts:
                raise ToolError("INVALID_TOOL_INPUT", "Unknown authoritative artifact handle.")
            path = self.artifacts[artifact]
            return (
                path,
                {
                    "requestedPath": explicit or "artifact:" + artifact,
                    "resolvedPath": path,
                    "reason": "artifact_handle",
                },
                None,
            )
        if not explicit:
            raise ToolError(
                "INVALID_TOOL_INPUT",
                "query_json requires an artifact handle or workspace-relative path.",
            )
        alias = self.alias_resolver(explicit) if self.alias_resolver else None
        if self.finance:
            normalized = explicit.strip().lstrip("/").removeprefix("./")
            if re.fullmatch(
                r"(?:public/data/(?:dashboard(?:-data)?|\d{6}(?:\.(?:sh|sz))?)|data/dashboard(?:-data)?)\.json",
                normalized,
                re.I,
            ):
                symbol = re.fullmatch(
                    r"public/data/(\d{6})(?:\.(?:sh|sz))?\.json", normalized, re.I
                )
                alias = {
                    "artifactId": "final_dashboard",
                    "requestedIdentity": symbol[1] if symbol else None,
                }
        if alias:
            path = self.artifacts.get(alias["artifactId"])
            if not path:
                raise ToolError(
                    "INVALID_TOOL_CONFIGURATION", "Alias resolved an unregistered artifact."
                )
            return (
                path,
                {
                    "requestedPath": explicit,
                    "resolvedPath": path,
                    "reason": "recognized_artifact_alias",
                },
                alias.get("requestedIdentity"),
            )
        return explicit, None, None

    async def _execute(self, name, body, context):
        if name == "query_json":
            return self.query_json(body)
        if name == "query_text_file":
            return self.query_text(body)
        return await super()._execute(name, body, context)

    def query_json(self, body):
        path, correction, identity = self.json_reference(body)
        pointers = body.get("pointers", body.get("pointer", [""]))
        pointers = [pointers] if isinstance(pointers, str) else pointers
        if (
            not isinstance(pointers, list)
            or not 1 <= len(pointers) <= 64
            or any(
                not isinstance(pointer, str)
                or js_length(pointer) > 512
                or any(char in pointer for char in "\0\r\n")
                for pointer in pointers
            )
        ):
            raise ToolError(
                "INVALID_TOOL_INPUT",
                "pointers must contain between 1 and 64 bounded single-line strings.",
            )
        pointers = list(dict.fromkeys(pointers))[:16]
        pointers = [
            pointer.strip()
            if not pointer.strip() or pointer.strip().startswith("/")
            else "/"
            + (
                pointer.strip()[2:] if pointer.strip().startswith("$.") else pointer.strip()
            ).replace(".", "/")
            for pointer in pointers
        ]
        for pointer in pointers:
            pointer_segments(pointer)
        settings = {
            key: integer(body, key, default, minimum, maximum)
            for key, default, minimum, maximum in (
                ("maxArrayItems", 8, 1, 50),
                ("maxStringChars", 600, 32, 4000),
                ("maxObjectKeys", 64, 1, 200),
                ("maxDepth", 8, 1, 16),
            )
        }
        try:
            resolved, content, data = self.read_text(path, raw=False)
        except ToolError as error:
            if error.code != "PATH_NOT_FOUND":
                raise
            suggestions = []
            for candidate in dict.fromkeys(self.artifacts.values()):
                try:
                    self.policy.read(candidate)
                    suggestions.append(candidate)
                except ToolError:
                    pass
            raise ToolError(
                "PATH_NOT_FOUND",
                "Workspace JSON path does not exist.",
                {
                    "requestedPath": correction["requestedPath"] if correction else path,
                    "suggestions": suggestions,
                    "retry": {"artifactPath": suggestions[0]}
                    if len(suggestions) == 1
                    else {"chooseFrom": suggestions},
                },
            ) from error
        try:
            root = json.loads(content)
        except ValueError as error:
            raise ToolError("INVALID_JSON_FILE", "Cannot query invalid JSON.") from error
        if identity:
            if self.alias_validator:
                identities = self.alias_validator(root, identity)
            else:
                available = set()

                def add(value):
                    match = (
                        re.search(r"\d{6}", str(value))
                        if type(value) in {str, int, float}
                        else None
                    )
                    if match:
                        available.add(match[0])

                def asset(record):
                    if not isinstance(record, dict):
                        return
                    add(record.get("symbol"))
                    add(record.get("code"))
                    if isinstance(record.get("quote"), dict):
                        add(record["quote"].get("symbol"))
                        add(record["quote"].get("code"))

                asset(root)
                if isinstance(root, dict) and isinstance(root.get("assets"), list):
                    for item in root["assets"]:
                        asset(item)
                identities = {
                    "matches": identity in available,
                    "availableIdentities": sorted(available),
                }
            if not identities["matches"]:
                raise ToolError(
                    "ARTIFACT_IDENTITY_MISMATCH",
                    "Requested alias identity does not match the authoritative artifact.",
                    {
                        "requestedPath": correction["requestedPath"],
                        "resolvedPath": resolved.relative,
                        "requestedIdentity": identity,
                        "availableIdentities": identities["availableIdentities"],
                    },
                )
        file = {
            "path": resolved.relative,
            "bytes": len(data),
            "sha256": sha(data),
            **({"pathCorrection": correction} if correction else {}),
        }
        rendered, truncated = bounded_json(
            file, root, pointers, settings, self.max_output, self.preferred
        )
        return {
            "ok": True,
            "data": {
                "path": resolved.relative,
                **(
                    {
                        "requestedPath": correction["requestedPath"],
                        "resolvedPath": correction["resolvedPath"],
                        "pathResolved": True,
                        "pathCorrected": correction["reason"] == "recognized_artifact_alias",
                        "correctionReason": correction["reason"],
                    }
                    if correction
                    else {}
                ),
                "bytes": len(data),
                "sha256": sha(data),
                "queryCount": len(pointers),
                "truncated": truncated,
            },
            "content": rendered,
        }

    def query_text(self, body):
        path = string(body, "path", maximum=1024)
        if path.lower().endswith(".json"):
            raise ToolError("STRUCTURED_JSON_QUERY_REQUIRED", "Use query_json for JSON files.")
        resolved, content, data = self.read_text(path, raw=False)
        raw = body.get("anchors", body.get("queries", body.get("query", [])))
        raw = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        anchors = []
        for value in raw:
            if not isinstance(value, str):
                continue
            for line in re.split(r"\r?\n", value.strip()):
                if line.strip() and not line.strip().startswith("```"):
                    anchors.append(js_slice(line.strip(), 0, 200))
                if len(anchors) >= 16:
                    break
            if len(anchors) >= 16:
                break
        anchors = list(dict.fromkeys(anchors)) or [""]
        sensitive = body.get("caseSensitive", True)
        sensitive = (
            sensitive
            if type(sensitive) is bool
            else sensitive.strip().lower() == "true"
            if isinstance(sensitive, str) and sensitive.strip().lower() in {"true", "false"}
            else True
        )
        before = tolerant_int(body, ["beforeLines"], 3, 0, 50)
        after = tolerant_int(body, ["afterLines"], 48, 0, 200)
        maximum = tolerant_int(body, ["maxMatchesPerAnchor", "maxMatchesPerQuery"], 2, 1, 10)
        lines = re.split(r"\r?\n", content)
        discovered = [
            {
                "anchor": anchor,
                "lines": [
                    index + 1
                    for index, line in enumerate(lines)
                    if (anchor if sensitive else anchor.lower())
                    in (line if sensitive else line.lower())
                ][:maximum],
            }
            for anchor in anchors
        ]
        selected = []
        for index in range(maximum):
            for item in discovered:
                if index < len(item["lines"]) and len(selected) < 32:
                    selected.append({"anchor": item["anchor"], "line": item["lines"][index]})
        rendered, windows, scale, truncated = "No matches.", [], 1, False
        if selected:
            for scale in (1, 0.75, 0.5, 0.33, 0.2, 0.1, 0):
                raw_windows = [
                    {
                        "start": max(1, match["line"] - math.floor(before * scale)),
                        "end": min(len(lines), match["line"] + math.floor(after * scale)),
                        "matches": [match],
                    }
                    for match in selected
                ]
                windows = []
                for window in sorted(raw_windows, key=lambda item: (item["start"], item["end"])):
                    if not windows or window["start"] > windows[-1]["end"] + 1:
                        windows.append(window)
                    else:
                        windows[-1]["end"] = max(windows[-1]["end"], window["end"])
                        windows[-1]["matches"].extend(window["matches"])
                text = [
                    f"[window {index + 1}: {resolved.relative}:{window['start']}-{window['end']}; "
                    + ", ".join(f"{match['anchor']}@{match['line']}" for match in window["matches"])
                    + "]\n"
                    + "\n".join(
                        f"{window['start'] + offset}: {line}"
                        for offset, line in enumerate(lines[window["start"] - 1 : window["end"]])
                    )
                    for index, window in enumerate(windows)
                ]
                rendered = "\n\n".join(text)
                if js_length(rendered) <= self.max_output:
                    truncated = scale < 1
                    break
                if scale == 0:
                    per_window = max(0, self.max_output - max(0, len(text) - 1) * 2) // len(text)
                    rendered = truncate(
                        "\n\n".join(truncate(item, per_window)["text"] for item in text)
                        if per_window > 0
                        else "",
                        self.max_output,
                    )["text"]
                    truncated = True
        selected_by = {
            anchor: [match["line"] for match in selected if match["anchor"] == anchor]
            for anchor in anchors
        }
        return {
            "ok": True,
            "data": {
                "path": resolved.relative,
                "bytes": len(data),
                "sha256": sha(data),
                "totalLines": len(lines),
                "anchorMatches": [
                    {
                        "anchor": item["anchor"],
                        "lines": selected_by[item["anchor"]],
                        "omittedMatches": len(item["lines"]) - len(selected_by[item["anchor"]]),
                    }
                    for item in discovered
                ],
                "selectedMatchCount": len(selected),
                "matchesTruncated": sum(len(item["lines"]) for item in discovered) > len(selected),
                "windowCount": len(windows),
                "contextScale": scale,
                "truncated": truncated,
            },
            "content": rendered,
        }
