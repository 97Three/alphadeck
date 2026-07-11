import hashlib
import json
import os
import re
import stat
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .provenance import canonical_json, provenance_hash
from .sessions import iso

MAX_FILE = 16777216
MAX_TOTAL = 134217728
MAX_FILES = 4096
MAX_PREVIEW = 2097152
EXCLUDED = {".next", "node_modules", ".git", ".data-agent"}


class EvidenceError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def digest(data):
    return "sha256:" + hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def lexical(value):
    return value.encode("utf-16-be", errors="surrogatepass")


def child(root, target):
    return target != root and target.is_relative_to(root)


def canonical_surface(root, path):
    try:
        canonical = path.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as error:
        raise EvidenceError(
            "ARTIFACT_SURFACE_CHANGED", "Acceptance surface changed while enumerating."
        ) from error
    if not child(root, canonical):
        raise EvidenceError(
            "ARTIFACT_SYMLINK_ESCAPE", "Acceptance artifact resolves outside the workspace."
        )
    if EXCLUDED.intersection(canonical.relative_to(root).parts):
        raise EvidenceError(
            "ARTIFACT_FORBIDDEN_PATH", "Acceptance artifact resolves into an excluded directory."
        )
    return canonical


def expand(root, requirements):
    result = {}

    def merge(requirement, path):
        candidate = {**requirement, "path": path}
        existing = result.get(path)

        def priority(item):
            return {"subject": 3, "evidence": 2, "control": 1}[item["role"]] * 2 + int(
                item["required"]
            )

        if existing:
            winner = candidate if priority(candidate) > priority(existing) else existing
            candidate = {**winner, "required": candidate["required"] or existing["required"]}
        result[path] = candidate
        if len(result) > MAX_FILES:
            raise EvidenceError(
                "ARTIFACT_FILE_COUNT_LIMIT_EXCEEDED",
                "Acceptance surface exceeds its file count budget.",
            )

    def walk(directory, logical, requirement, ancestors):
        for entry in sorted(directory.iterdir(), key=lambda path: lexical(path.name)):
            if entry.name in EXCLUDED:
                continue
            logical_child = logical + "/" + entry.name
            canonical = canonical_surface(root, entry)
            info = canonical.stat()
            if stat.S_ISREG(info.st_mode):
                merge(requirement, logical_child)
            elif stat.S_ISDIR(info.st_mode):
                if canonical in ancestors:
                    raise EvidenceError(
                        "ARTIFACT_SYMLINK_CYCLE", "Acceptance surface contains a symlink cycle."
                    )
                walk(canonical, logical_child, requirement, ancestors | {canonical})
            else:
                raise EvidenceError(
                    "ARTIFACT_UNSUPPORTED_FILE_TYPE",
                    "Acceptance surface contains a non-regular file.",
                )

    for requirement in sorted(requirements, key=lambda item: lexical(item["path"])):
        relative = requirement["path"]
        if requirement["role"] == "control":
            continue
        if relative.endswith("/**"):
            logical = relative[:-3]
            path = Path(os.path.abspath(root / logical))
            if not child(root, path):
                raise EvidenceError(
                    "ARTIFACT_PATH_ESCAPE", "Acceptance surface escapes the workspace."
                )
            if not path.exists() and not path.is_symlink() and not requirement["required"]:
                continue
            canonical = canonical_surface(root, path)
            if not canonical.is_dir():
                raise EvidenceError(
                    "ARTIFACT_SURFACE_NOT_DIRECTORY",
                    "Recursive acceptance surface is not a directory.",
                )
            walk(canonical, logical, requirement, {canonical})
        elif "*" in relative:
            if "/" in relative:
                raise EvidenceError(
                    "ARTIFACT_PATTERN_INVALID", "Only root-file wildcard patterns are supported."
                )
            pattern = re.compile(
                "^" + "[^/]*".join(re.escape(part) for part in relative.split("*")) + "$"
            )
            for path in sorted(root.iterdir(), key=lambda path: lexical(path.name)):
                if pattern.fullmatch(path.name):
                    canonical = canonical_surface(root, path)
                    if not canonical.is_file():
                        raise EvidenceError(
                            "ARTIFACT_SURFACE_NOT_FILE", "Root acceptance artifact is not a file."
                        )
                    merge(requirement, path.name)
        else:
            merge(requirement, relative)
    return sorted(result.values(), key=lambda item: lexical(item["path"]))


def read_file(root, relative):
    candidate = Path(os.path.abspath(root / relative))
    if not child(root, candidate):
        raise EvidenceError("ARTIFACT_PATH_ESCAPE", "Evidence artifact escapes the workspace.")
    try:
        canonical = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as error:
        raise EvidenceError(
            "REQUIRED_ARTIFACT_MISSING", "Required evidence artifact is missing."
        ) from error
    if not child(root, canonical):
        raise EvidenceError(
            "ARTIFACT_SYMLINK_ESCAPE", "Evidence artifact resolves outside the workspace."
        )
    info = canonical.stat()
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceError(
            "REQUIRED_ARTIFACT_NOT_FILE", "Evidence artifact is not a regular file."
        )
    if info.st_size > MAX_FILE:
        raise EvidenceError(
            "EVIDENCE_ARTIFACT_TOO_LARGE", "Evidence artifact exceeds its file size budget."
        )
    descriptor = os.open(
        canonical, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise EvidenceError(
                "ARTIFACT_SURFACE_CHANGED", "Evidence artifact changed during verification."
            )
        proc = Path("/proc/self/fd") / str(stream.fileno())
        if proc.exists() and not child(root, proc.resolve(strict=True)):
            raise EvidenceError(
                "ARTIFACT_SYMLINK_ESCAPE", "Evidence descriptor escaped the workspace."
            )
        content = stream.read(MAX_FILE + 1)
    if len(content) > MAX_FILE:
        raise EvidenceError(
            "EVIDENCE_ARTIFACT_TOO_LARGE", "Evidence artifact exceeds its file size budget."
        )
    return {"content": content, "bytes": len(content), "sha256": digest(content)}


def manifest(root, requirements):
    subjects, evidence, issues = [], [], []
    total, count = 0, 0
    for requirement in expand(root, requirements):
        try:
            file = read_file(root, requirement["path"])
        except EvidenceError as error:
            if error.code not in {"REQUIRED_ARTIFACT_MISSING", "REQUIRED_ARTIFACT_NOT_FILE"}:
                raise
            if requirement["required"]:
                issues.append(
                    {"path": requirement["path"], "role": requirement["role"], "code": error.code}
                )
            continue
        count += 1
        total += file["bytes"]
        if count > MAX_FILES:
            raise EvidenceError(
                "ARTIFACT_FILE_COUNT_LIMIT_EXCEEDED",
                "Acceptance surface exceeds its file count budget.",
            )
        if total > MAX_TOTAL:
            raise EvidenceError(
                "ARTIFACT_TOTAL_BYTES_LIMIT_EXCEEDED",
                "Acceptance surface exceeds its total size budget.",
            )
        item = {
            "path": requirement["path"],
            "role": requirement["role"],
            "bytes": file["bytes"],
            "sha256": file["sha256"],
        }
        (subjects if requirement["role"] == "subject" else evidence).append(item)
    return {"subjects": subjects, "evidence": evidence, "issues": issues}


def checks(value):
    if not isinstance(value, list):
        raise EvidenceError("VALIDATION_CHECKS_INVALID", "Validation checks must be an array.")
    output = []
    for item in value:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or item.get("status") not in {"passed", "failed", "warning"}
        ):
            raise EvidenceError(
                "VALIDATION_CHECK_INVALID", "Validation report contains a malformed check."
            )
        output.append(
            {
                "id": item["id"],
                "status": item["status"],
                "summarySha256": digest(
                    item["summary"] if isinstance(item.get("summary"), str) else ""
                ),
            }
        )
    return output


def classification(spec, report, items):
    def failure(verdict, codes, failed):
        return {
            "verdict": verdict,
            "reasonCodes": codes,
            "failedCheckIds": sorted(failed, key=lexical),
        }

    counts = Counter(item["id"] for item in items)
    missing = [key for key in spec["requiredValidationCheckIds"] if key not in counts]
    duplicate = [key for key, value in counts.items() if value > 1]
    if missing or duplicate:
        return failure(
            "rejected",
            (["REQUIRED_VALIDATION_CHECK_MISSING"] if missing else [])
            + (["VALIDATION_CHECK_DUPLICATED"] if duplicate else []),
            missing + duplicate,
        )
    required = [item for item in items if item["id"] in spec["requiredValidationCheckIds"]]
    failed = [item["id"] for item in required if item["status"] == "failed"]
    warnings = [
        item["id"]
        for item in required
        if item["status"] == "warning" and item["id"] not in spec["allowedValidationWarnings"]
    ]
    passed = report.get("passed") is True and report.get("status") == "passed"
    if passed and any(item["status"] == "failed" for item in items):
        return failure(
            "rejected",
            ["VALIDATION_REPORT_CONTRADICTS_CHECKS"],
            [item["id"] for item in items if item["status"] == "failed"],
        )
    if not passed and not failed and not warnings:
        return failure("rejected", ["VALIDATION_REPORT_CONTRADICTS_CHECKS"], [])
    if failed:
        return failure("repair_required", ["REQUIRED_VALIDATION_CHECK_FAILED"], failed)
    if warnings:
        infrastructure = bool(
            {"visual_presentation", "preview_http_200", "vue_build"}.intersection(warnings)
        )
        return failure(
            "retry_infrastructure" if infrastructure else "repair_required",
            [
                "REQUIRED_VALIDATION_INFRASTRUCTURE_WARNING"
                if infrastructure
                else "REQUIRED_VALIDATION_WARNING"
            ],
            warnings,
        )
    return None


async def probe(client, url, port):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
        ):
            raise EvidenceError(
                "PREVIEW_URL_NOT_LOCAL",
                "Only the platform-managed local HTTP preview can be verified.",
            )
        if type(port) is not int or port <= 0 or (parsed.port or 80) != port:
            raise EvidenceError(
                "PREVIEW_PORT_MISMATCH", "Preview URL does not match the platform-reported port."
            )
    except ValueError as error:
        raise EvidenceError("PREVIEW_URL_NOT_LOCAL", "Preview URL is invalid.") from error
    try:
        async with client.stream(
            "GET", url, headers={"Accept": "text/html"}, follow_redirects=False, timeout=15
        ) as response:
            if response.is_redirect:
                raise EvidenceError(
                    "PREVIEW_HTTP_UNAVAILABLE", "Persistent preview returned a redirect."
                )
            try:
                declared = int(response.headers.get("content-length", "0"))
            except ValueError:
                declared = 0
            if declared > MAX_PREVIEW:
                raise EvidenceError(
                    "PREVIEW_RESPONSE_TOO_LARGE", "Preview exceeds its response budget."
                )
            content = bytearray()
            async for data in response.aiter_bytes():
                if len(content) + len(data) > MAX_PREVIEW:
                    raise EvidenceError(
                        "PREVIEW_RESPONSE_TOO_LARGE", "Preview exceeds its response budget."
                    )
                content.extend(data)
            if response.status_code != 200:
                raise EvidenceError(
                    "PREVIEW_HTTP_NOT_READY",
                    f"Persistent preview returned HTTP {response.status_code}.",
                )
            return {"status": 200, "responseSha256": digest(content)}
    except httpx.HTTPError as error:
        raise EvidenceError(
            "PREVIEW_HTTP_UNAVAILABLE", "Persistent preview HTTP probe failed."
        ) from error


async def verify(input, *, client=None, now=None):
    if client is None:
        async with httpx.AsyncClient(trust_env=False) as owned:
            return await verify(input, client=owned, now=now)
    version = input["candidateVersion"]
    if type(version) is not int or not 0 < version <= 9007199254740991:
        raise EvidenceError(
            "CANDIDATE_VERSION_INVALID", "A positive candidate version is required."
        )
    spec = input["missionSpec"]
    supplied = input["missionSpecSha256"].removeprefix("sha256:").lower()
    if not re.fullmatch(r"[a-f0-9]{64}", supplied) or supplied != provenance_hash(spec):
        raise EvidenceError(
            "MISSION_SPEC_HASH_MISMATCH", "Mission spec does not match its durable hash."
        )
    root = Path(input["workspaceRoot"]).resolve(strict=True)
    report_path = spec["validationReportPath"]
    unavailable = {"content": b"", "bytes": 0, "sha256": digest("validation-report-unavailable")}
    report, items = {}, []
    report_available = True
    failure = None
    try:
        first_report = read_file(root, report_path)
    except EvidenceError as error:
        if error.code not in {
            "REQUIRED_ARTIFACT_MISSING",
            "REQUIRED_ARTIFACT_NOT_FILE",
            "EVIDENCE_ARTIFACT_TOO_LARGE",
        }:
            raise
        first_report = unavailable
        report_available = False
        failure = {
            "verdict": "retry_infrastructure",
            "reasonCodes": ["VALIDATION_REPORT_UNAVAILABLE"],
            "failedCheckIds": [],
        }
    if failure is None:
        try:
            parsed = json.loads(first_report["content"].decode(errors="replace"))
            if not isinstance(parsed, dict):
                raise ValueError("Validation report must be an object")
            report = parsed
            items = checks(report.get("checks"))
        except (ValueError, EvidenceError) as error:
            failure = {
                "verdict": "rejected",
                "reasonCodes": [
                    error.code
                    if isinstance(error, EvidenceError)
                    else "VALIDATION_REPORT_INVALID_JSON"
                ],
                "failedCheckIds": [],
            }
    run = report.get("runId") if isinstance(report.get("runId"), str) else None
    project = report.get("projectId") if isinstance(report.get("projectId"), str) else None
    validation_failure = failure or classification(spec, report, items)
    run_mismatch = failure is None and run != spec["requestId"]
    project_mismatch = failure is None and project != spec["projectId"]
    first = manifest(root, spec["artifacts"])
    preview = {"status": 0, "responseSha256": digest("not-probed")}
    preview_error = None
    if not validation_failure and not run_mismatch and not project_mismatch and not first["issues"]:
        for attempt in range(2):
            try:
                preview = await probe(client, input["preview"]["url"], input["preview"]["port"])
                break
            except EvidenceError as error:
                if attempt == 0 and error.code in {
                    "PREVIEW_HTTP_UNAVAILABLE",
                    "PREVIEW_HTTP_NOT_READY",
                }:
                    continue
                preview_error = error
                break
    report_available_after = True
    try:
        second_report = read_file(root, report_path)
    except EvidenceError as error:
        if error.code not in {"REQUIRED_ARTIFACT_MISSING", "REQUIRED_ARTIFACT_NOT_FILE"}:
            raise
        second_report = unavailable
        report_available_after = False
    report_changed = (
        report_available != report_available_after
        or first_report["sha256"] != second_report["sha256"]
    )
    authoritative_run, authoritative_checks = run, items
    if report_changed:
        authoritative_run, authoritative_checks = None, []
        if report_available_after:
            try:
                replacement = json.loads(second_report["content"].decode(errors="replace"))
                if isinstance(replacement, dict):
                    authoritative_run = (
                        replacement.get("runId")
                        if isinstance(replacement.get("runId"), str)
                        else None
                    )
                    authoritative_checks = checks(replacement.get("checks"))
            except (ValueError, EvidenceError):
                pass
    second = manifest(root, spec["artifacts"])
    subject_hash = "sha256:" + provenance_hash(second["subjects"])
    evidence_hash = "sha256:" + provenance_hash(second["evidence"])
    manifested = next((item for item in second["evidence"] if item["path"] == report_path), None)
    if bool(manifested) != report_available_after or (
        manifested and manifested["sha256"] != second_report["sha256"]
    ):
        report_changed = True
    subject_changed = first["subjects"] != second["subjects"]
    evidence_changed = first["evidence"] != second["evidence"]
    issues_changed = first["issues"] != second["issues"]
    artifact_codes = (
        ["REQUIRED_SUBJECT_ARTIFACT_UNAVAILABLE"]
        if any(issue["role"] == "subject" for issue in second["issues"])
        else []
    ) + (
        ["REQUIRED_DERIVED_EVIDENCE_UNAVAILABLE"]
        if any(issue["role"] == "evidence" for issue in second["issues"])
        else []
    )
    verdict, reasons, failed = "accepted", [], []
    if project_mismatch:
        verdict, reasons = "stale", ["VALIDATION_PROJECT_ID_MISMATCH"]
    elif run_mismatch:
        verdict, reasons = "stale", ["VALIDATION_RUN_ID_MISMATCH"]
    elif subject_changed or (
        issues_changed and any(issue["role"] == "subject" for issue in second["issues"])
    ):
        verdict, reasons = "stale", ["SUBJECT_MANIFEST_CHANGED_DURING_VERIFICATION"]
    elif report_changed:
        verdict, reasons = "stale", ["VALIDATION_REPORT_CHANGED_DURING_VERIFICATION"]
    elif evidence_changed or issues_changed:
        verdict, reasons = "stale", ["EVIDENCE_MANIFEST_CHANGED_DURING_VERIFICATION"]
    elif validation_failure:
        verdict = validation_failure["verdict"]
        reasons = list(dict.fromkeys(validation_failure["reasonCodes"] + artifact_codes))
        failed = validation_failure["failedCheckIds"]
    elif second["issues"]:
        verdict, reasons = "repair_required", artifact_codes
    elif preview_error:
        verdict, reasons = "retry_infrastructure", [preview_error.code]
    created = iso(now() if now else datetime.now(UTC))
    payload = {
        "schemaVersion": 1,
        "missionId": input["missionId"],
        "generationId": input["generationId"],
        "projectId": spec["projectId"],
        "requestId": spec["requestId"],
        "candidateVersion": version,
        "missionSpecSha256": "sha256:" + supplied,
        "validation": {
            "reportPath": report_path,
            "reportSha256": second_report["sha256"],
            "runId": authoritative_run,
            "checks": authoritative_checks,
        },
        "artifacts": {
            "subjectManifestSha256": subject_hash,
            "evidenceManifestSha256": evidence_hash,
            "items": second["subjects"],
            "evidenceItems": second["evidence"],
            "issues": second["issues"],
        },
        "preview": {
            "url": input["preview"]["url"],
            "port": input["preview"]["port"],
            "httpStatus": preview["status"],
            "responseSha256": preview["responseSha256"],
            "readyAt": created,
        },
        "decision": {"verdict": verdict, "reasonCodes": reasons, "failedCheckIds": failed},
        "createdAt": created,
    }
    return {
        "verdict": verdict,
        "reasonCodes": reasons,
        "failedCheckIds": failed,
        "candidateVersion": version,
        "subjectHash": subject_hash,
        "payload": payload,
        "receiptHash": digest(canonical_json(payload)),
    }
