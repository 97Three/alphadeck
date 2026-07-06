"""Framework-owned, bounded facts retained when raw tool history is compacted."""

import hashlib
import json
import re
from copy import deepcopy

from .messages import js_length
from .provenance import canonical_json, js_json

PREFIX = "[PI Agent Trusted Context Capsule v1]\n"
EFFECTS = {"pure", "read", "workspace_write", "external_write"}
TARGET_KEYS = {
    "artifact",
    "artifactPath",
    "artifacts",
    "canonicalRelativePath",
    "changedFiles",
    "path",
    "paths",
    "requestedPath",
    "resolvedPath",
    "targetPath",
    "verifiedArtifacts",
}


class ContextError(Exception):
    def __init__(self, code, message, details=None):
        self.code, self.message, self.details = code, message, details or {}
        super().__init__(message)


def utf8(value):
    # TextEncoder replaces unpaired UTF-16 surrogates with U+FFFD.
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace").encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def safe_integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9007199254740991


def identifier(value, label, maximum=512):
    if (
        not isinstance(value, str)
        or not value
        or js_length(value) > maximum
        or re.search(r"[\x00-\x1f\x7f]", value)
    ):
        raise ContextError(
            "INVALID_CONTEXT_CAPSULE",
            f"Invalid {label} supplied to the trusted context capsule.",
            {"field": label},
        )
    return value


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def normalize_target(value, key):
    candidate = value.strip().replace("\\", "/")
    if (
        not candidate
        or js_length(candidate) > 1024
        or re.search(r"[\x00-\x1f\x7f]", candidate)
        or re.match(r"[a-zA-Z]:/", candidate)
        or candidate.startswith("//")
        or ".." in candidate.split("/")
    ):
        return None
    if key == "artifact" and not candidate.startswith("artifact:"):
        return "artifact:" + candidate
    if candidate.startswith("/api/"):
        return "endpoint:" + candidate
    if candidate.startswith("/"):
        return None
    return candidate[2:] if candidate.startswith("./") else candidate


def js_sorted(values):
    return sorted(values, key=lambda value: value.encode("utf-16-be", "surrogatepass"))


def collect_targets(*sources):
    output, visited = set(), 0

    def visit(value, depth=0, parent=""):
        nonlocal visited
        if visited >= 512 or len(output) >= 64 or depth > 4:
            return
        visited += 1
        if isinstance(value, str):
            normalized = normalize_target(value, parent) if parent in TARGET_KEYS else None
            if normalized:
                output.add(normalized)
        elif isinstance(value, list):
            if parent in TARGET_KEYS:
                for item in value:
                    visit(item, depth + 1, parent)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in TARGET_KEYS or (depth < 4 and isinstance(child, dict)):
                    visit(child, depth + 1, key)

    for source in sources:
        visit(source)
    return js_sorted(output)


def telemetry(body, checkpoint):
    tombstones = body["operationTombstones"]
    return {
        "version": 1,
        "phase": body["phase"],
        "sha256": checkpoint["sha256"],
        "serializedUtf8Bytes": checkpoint["serializedUtf8Bytes"],
        "coveredToolCalls": len(checkpoint["coveredToolCallIds"]),
        "targetReferences": len(body["targetReferences"]),
        "operationTombstones": len(tombstones),
        "rolledUpOperationTombstones": (body["operationTombstoneRollup"] or {}).get("count", 0),
        "frameworkOutcomeTombstones": sum(
            item["source"] == "framework_outcome" for item in tombstones
        ),
        **{
            key: len(body[key])
            for key in ("artifactReceipts", "readReceipts", "successfulWrites", "remainingFailures")
        },
        "invalidatedReadReceipts": body["invalidatedReadReceipts"],
    }


class CapsuleSession:
    def __init__(self, max_utf8_bytes=8192):
        if not safe_integer(max_utf8_bytes, 256):
            raise ContextError(
                "INVALID_CONTEXT_CAPSULE",
                "Context capsule maxUtf8Bytes must be a safe integer of at least 256.",
                {"field": "maxUtf8Bytes", "value": max_utf8_bytes},
            )
        self.maximum = max_utf8_bytes
        self.tombstones, self.rollup = {}, None
        self.artifacts, self.reads, self.invalidated, self.writes, self.failures = (
            {},
            {},
            {},
            {},
            {},
        )
        self.generation = self.invalidated_reads = 0

    def _identity(self, operation):
        for key in ("operationId", "toolCallId", "toolName"):
            identifier(operation.get(key), key, 256 if key == "toolName" else 512)
        if not safe_integer(operation.get("turn"), 1) or not sha(operation.get("resultSha256")):
            raise ContextError(
                "INVALID_CONTEXT_CAPSULE",
                "Context capsule operation requires a valid turn and SHA-256 receipt.",
            )

    def _invalidate(self):
        self.generation += 1
        self.invalidated_reads += len(self.reads)
        self.invalidated.update(self.reads)
        self.reads.clear()
        for key, receipt in list(self.artifacts.items()):
            if receipt["workspaceGeneration"] < self.generation:
                self.invalidated[key] = self.artifacts.pop(key)

    def _roll(self):
        if not self.tombstones:
            return False
        identity = next(iter(self.tombstones))
        oldest = self.tombstones.pop(identity)
        for receipts in (self.artifacts, self.reads, self.invalidated):
            for key, receipt in list(receipts.items()):
                if receipt["operationId"] == identity:
                    del receipts[key]
        self.writes.pop(identity, None)
        self.failures.pop(identity, None)
        previous = self.rollup or {}
        self.rollup = {
            "count": previous.get("count", 0) + 1,
            "throughTurn": max(previous.get("throughTurn", 0), oldest["turn"]),
            "sha256": hashlib.sha256(
                utf8(previous.get("sha256", "") + "\0") + canonical_json(oldest)
            ).hexdigest(),
            **{
                key: previous.get(key, 0) + int(oldest[field] == expected)
                for key, field, expected in (
                    ("succeeded", "status", "succeeded"),
                    ("failed", "status", "failed"),
                    ("pure", "effect", "pure"),
                    ("reads", "effect", "read"),
                    ("workspaceWrites", "effect", "workspace_write"),
                    ("externalWrites", "effect", "external_write"),
                )
            },
        }
        return True

    def _tombstone(self, operation, tombstone):
        identity = operation["operationId"]
        if identity in self.tombstones and canonical_json(
            self.tombstones[identity]
        ) != canonical_json(tombstone):
            raise ContextError(
                "INVALID_CONTEXT_CAPSULE",
                "A framework operation ID cannot be reused for a different context fact.",
                {"field": "operationId"},
            )
        self.tombstones[identity] = tombstone
        while len(self.tombstones) > 20:
            self._roll()

    def record(self, operation):
        self._identity(operation)
        targets = collect_targets({"paths": operation["targetReferences"]})
        result = operation["result"]
        common = {key: operation[key] for key in ("toolCallId", "toolName", "turn", "effect")}
        if not result["ok"]:
            code = identifier(result["error"]["code"], "failureCode", 256)
            self.failures[operation["operationId"]] = {
                "operationId": operation["operationId"],
                "toolName": operation["toolName"],
                "turn": operation["turn"],
                "effect": operation["effect"],
                "code": code,
                "targets": targets,
            }
            self._tombstone(
                operation,
                {
                    **common,
                    "status": "failed",
                    "terminal": False,
                    "targets": targets,
                    "resultSha256": operation["resultSha256"],
                    "workspaceGeneration": self.generation,
                    "source": "trusted_receipt",
                    "failureCode": code,
                },
            )
            return
        for key, failure in list(self.failures.items()):
            same_tool = failure["toolName"] == operation["toolName"]
            overlaps = bool(set(failure["targets"]) & set(operation["targetReferences"]))
            if (
                same_tool
                if not failure["targets"] or not operation["targetReferences"]
                else overlaps and (same_tool or operation["effect"] == "workspace_write")
            ):
                del self.failures[key]
        if operation["effect"] == "workspace_write":
            self._invalidate()
        data = result.get("data") or {}
        metadata = {}
        if "artifactSha256" in operation:
            if not sha(operation["artifactSha256"]):
                raise ContextError("INVALID_CONTEXT_CAPSULE", "Invalid trusted artifact digest.")
            metadata["artifactSha256"] = operation["artifactSha256"]
        if "bytes" in operation:
            if not safe_integer(operation["bytes"]):
                raise ContextError(
                    "INVALID_CONTEXT_CAPSULE", "Invalid trusted artifact byte count."
                )
            metadata["bytes"] = operation["bytes"]
        if isinstance(data, dict):
            if "artifactSha256" not in metadata and sha(data.get("sha256")):
                metadata["artifactSha256"] = data["sha256"]
            if "bytes" not in metadata and safe_integer(data.get("bytes")):
                metadata["bytes"] = data["bytes"]
        receipt = {
            "operationId": operation["operationId"],
            "toolName": operation["toolName"],
            "turn": operation["turn"],
            "targets": targets,
            "resultSha256": operation["resultSha256"],
            "workspaceGeneration": self.generation,
            **metadata,
        }
        key = receipt["toolName"] + "\0" + "\0".join(targets)
        if operation["effect"] == "read":
            self.reads[key] = receipt
        elif operation["effect"] != "workspace_write":
            self.artifacts[key] = receipt
        else:
            self.writes[operation["operationId"]] = receipt
        self._tombstone(
            operation,
            {
                **common,
                "status": "succeeded",
                "terminal": operation["terminal"],
                "targets": targets,
                "resultSha256": operation["resultSha256"],
                "workspaceGeneration": self.generation,
                "source": "trusted_receipt",
                **metadata,
            },
        )

    def record_framework_outcome(self, operation):
        self._identity(operation)
        if not sha(operation.get("targetIdentitySha256")):
            raise ContextError(
                "INVALID_CONTEXT_CAPSULE",
                "Framework context target identity requires a SHA-256 digest.",
                {"field": "targetIdentitySha256"},
            )
        if operation["status"] == "succeeded" and operation["effect"] == "workspace_write":
            self._invalidate()
        self._tombstone(
            operation,
            {
                **{
                    key: operation[key]
                    for key in ("toolCallId", "toolName", "turn", "effect", "status")
                },
                "terminal": operation["status"] == "succeeded" and operation["terminal"],
                "targets": [],
                "targetIdentitySha256": operation["targetIdentitySha256"],
                "resultSha256": operation["resultSha256"],
                "workspaceGeneration": self.generation,
                "source": "framework_outcome",
            },
        )

    def _evict(self):
        for receipts, keep in (
            (self.invalidated, 0),
            (self.reads, 0),
            (self.artifacts, 0),
            (self.writes, 1),
            (self.failures, 1),
            (self.writes, 0),
            (self.failures, 0),
        ):
            if len(receipts) > keep:
                del receipts[next(iter(receipts))]
                return True
        return False

    def checkpoint(self, phase):
        if phase == "exploration":
            return None
        if phase not in {"writing", "submission"}:
            raise ContextError("INVALID_CONTEXT_CAPSULE", "Invalid execution phase.")
        while True:
            tombstones = list(self.tombstones.values())
            targets = js_sorted({target for item in tombstones for target in item["targets"]})
            receipts = (self.artifacts, self.reads, self.invalidated, self.writes, self.failures)
            if sum(map(len, receipts)) > 64:
                if self._evict():
                    continue
                return None
            if len(targets) > 64:
                if len(tombstones) > 1 and self._roll():
                    continue
                return None
            body = {
                "phase": phase,
                "workspaceGeneration": self.generation,
                "invalidatedReadReceipts": self.invalidated_reads,
                "targetReferences": targets,
                "operationTombstoneRollup": self.rollup,
                "operationTombstones": tombstones,
                **{
                    key: list(value.values())
                    for key, value in zip(
                        (
                            "artifactReceipts",
                            "readReceipts",
                            "invalidatedReceipts",
                            "successfulWrites",
                            "remainingFailures",
                        ),
                        receipts,
                        strict=True,
                    )
                },
            }
            covered = js_sorted({item["toolCallId"] for item in tombstones})
            checksum = digest(
                {"body": body, "coveredToolCallIds": covered, "maxUtf8Bytes": self.maximum}
            )
            content = PREFIX + js_json(
                {
                    "$piAgent": {
                        "kind": "trusted_context_capsule",
                        "version": 1,
                        "generatedBy": "PiAgentContextCapsuleSession",
                        "digest": {"algorithm": "SHA-256", "hex": checksum},
                        "maxUtf8Bytes": self.maximum,
                    },
                    **body,
                }
            )
            size = len(utf8(content))
            if size > self.maximum:
                if self._evict() or (len(tombstones) > 1 and self._roll()):
                    continue
                return None
            checkpoint = {
                "version": 1,
                "phase": phase,
                "sha256": checksum,
                "content": content,
                "serializedUtf8Bytes": size,
                "coveredToolCallIds": covered,
            }
            checkpoint["telemetry"] = telemetry(body, checkpoint)
            return deepcopy(checkpoint)


def assert_capsule(checkpoint):
    """Verify facts without Python assertions, including when running with -O."""

    def require(condition):
        if not condition:
            raise ValueError("Invalid context capsule")

    try:
        content = checkpoint["content"]
        require(
            type(checkpoint["version"]) is int
            and checkpoint["version"] == 1
            and sha(checkpoint["sha256"])
        )
        require(isinstance(content, str) and content.startswith(PREFIX))
        require(
            safe_integer(checkpoint["serializedUtf8Bytes"])
            and len(utf8(content)) == checkpoint["serializedUtf8Bytes"]
        )
        body = json.loads(content[len(PREFIX) :])
        marker = body.pop("$piAgent")
        covered = checkpoint["coveredToolCallIds"]
        require(isinstance(covered, list) and covered == js_sorted(set(covered)))
        for call in covered:
            identifier(call, "toolCallId")
        require(body["phase"] == checkpoint["phase"] and body["phase"] in {"writing", "submission"})
        require(
            safe_integer(body["workspaceGeneration"])
            and safe_integer(body["invalidatedReadReceipts"])
        )
        require(isinstance(body["targetReferences"], list))
        for key in (
            "artifactReceipts",
            "readReceipts",
            "invalidatedReceipts",
            "successfulWrites",
            "remainingFailures",
        ):
            require(isinstance(body[key], list))
        tombstones = body["operationTombstones"]
        require(isinstance(tombstones, list) and len(tombstones) <= 20)
        for item in tombstones:
            identifier(item["toolCallId"], "toolCallId")
            identifier(item["toolName"], "toolName", 256)
            require(safe_integer(item["turn"], 1) and safe_integer(item["workspaceGeneration"]))
            require(item["effect"] in EFFECTS and item["status"] in {"succeeded", "failed"})
            require(type(item["terminal"]) is bool and isinstance(item["targets"], list))
            require(
                all(
                    isinstance(target, str) and normalize_target(target, "paths") == target
                    for target in item["targets"]
                )
            )
            require(
                sha(item["resultSha256"])
                and item["source"] in {"trusted_receipt", "framework_outcome"}
            )
            for key in ("targetIdentitySha256", "artifactSha256"):
                require(key not in item or sha(item[key]))
            require("bytes" not in item or safe_integer(item["bytes"]))
            if "failureCode" in item:
                identifier(item["failureCode"], "failureCode", 256)
        rollup = body["operationTombstoneRollup"]
        if rollup is not None:
            require(
                all(
                    safe_integer(rollup[key])
                    for key in (
                        "count",
                        "throughTurn",
                        "succeeded",
                        "failed",
                        "pure",
                        "reads",
                        "workspaceWrites",
                        "externalWrites",
                    )
                )
            )
            require(rollup["count"] > 0 and rollup["throughTurn"] > 0 and sha(rollup["sha256"]))
            require(rollup["succeeded"] + rollup["failed"] == rollup["count"])
            require(
                sum(rollup[key] for key in ("pure", "reads", "workspaceWrites", "externalWrites"))
                == rollup["count"]
            )
        require(covered == js_sorted({item["toolCallId"] for item in tombstones}))
        require(
            marker["kind"] == "trusted_context_capsule"
            and type(marker["version"]) is int
            and marker["version"] == 1
        )
        require(marker["generatedBy"] == "PiAgentContextCapsuleSession")
        require(marker["digest"] == {"algorithm": "SHA-256", "hex": checkpoint["sha256"]})
        require(safe_integer(marker["maxUtf8Bytes"], 256))
        require(
            digest(
                {
                    "body": body,
                    "coveredToolCallIds": covered,
                    "maxUtf8Bytes": marker["maxUtf8Bytes"],
                }
            )
            == checkpoint["sha256"]
        )
        require(checkpoint["serializedUtf8Bytes"] <= marker["maxUtf8Bytes"])
        require(
            canonical_json(checkpoint["telemetry"]) == canonical_json(telemetry(body, checkpoint))
        )
    except (KeyError, TypeError, ValueError, ContextError, AttributeError) as cause:
        raise ContextError(
            "INVALID_CONTEXT_CAPSULE",
            "Trusted context capsule version, digest, phase, or budget verification failed.",
        ) from cause
