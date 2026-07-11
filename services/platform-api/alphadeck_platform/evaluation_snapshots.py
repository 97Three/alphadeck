import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta

from .config import ROOT
from .evaluation_queue import date
from .finance_values import js_string
from .icu_collation import collator
from .provenance import js_json


def canonical_json(value, *, locale=None):
    if locale is None:
        locale = json.loads((ROOT / "config/evaluation-catalog.json").read_text()).get(
            "collationLocale", "en-US"
        )
    selected = collator(locale)

    def canonical(item):
        if isinstance(item, list):
            return [canonical(child) for child in item]
        if isinstance(item, dict):
            return {key: canonical(item[key]) for key in sorted(item, key=selected.key)}
        return item

    return js_json(canonical(value))


def payload_sha256(value, *, locale=None):
    return hashlib.sha256(canonical_json(value, locale=locale).encode()).hexdigest()


def attest_snapshot(snapshot, payload, *, expected_case=None, now=None):
    problems, record = [], payload if isinstance(payload, dict) else {}
    if type(snapshot.get("schemaVersion")) is not int or snapshot["schemaVersion"] != 1:
        problems.append("snapshot schemaVersion 必须为 1")
    if not isinstance(snapshot.get("id"), str) or not re.fullmatch(
        r"[A-Za-z0-9_:.-]+", snapshot["id"]
    ):
        problems.append("snapshot id 无效")
    if not snapshot.get("caseId"):
        problems.append("snapshot 缺少 caseId")
    if expected_case and snapshot.get("caseId") != expected_case:
        problems.append("snapshot caseId 应为 " + expected_case)
    if isinstance(record.get("caseId"), str) and record["caseId"] != snapshot.get("caseId"):
        problems.append("fixture caseId 与 snapshot caseId 不一致")
    if snapshot.get("datasetKind") not in ("market_response", "oracle_fixture"):
        problems.append("snapshot datasetKind 无效")
    path = snapshot.get("fixturePath", "")
    if not path or path.startswith("/") or ".." in path:
        problems.append("snapshot fixturePath 必须是仓库内安全相对路径")
    digest = snapshot.get("payloadSha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        problems.append("snapshot payloadSha256 无效")
    elif payload_sha256(payload) != digest:
        problems.append("snapshot payloadSha256 与 fixture 内容不一致")
    source = snapshot.get("source") or {}
    if not source.get("provider") or not source.get("version"):
        problems.append("snapshot 缺少 source provider/version")
    if not snapshot.get("tradingCalendarVersion"):
        problems.append("snapshot 缺少交易日历版本")
    if snapshot.get("adjustment") not in ("none", "qfq", "hfq", "mixed"):
        problems.append("snapshot adjustment 无效")
    observation = snapshot.get("observation") or {}
    as_of, captured, minimum, maximum = [
        date(value) if isinstance(value, str) else None
        for value in (
            snapshot.get("asOf"),
            snapshot.get("capturedAt"),
            observation.get("minAt"),
            observation.get("maxAt"),
        )
    ]
    if any(value is None for value in (as_of, captured, minimum, maximum)):
        problems.append("snapshot 时间字段必须是有效 ISO 时间")
    else:
        if minimum > maximum:
            problems.append("snapshot observation.minAt 晚于 maxAt")
        if maximum > as_of:
            problems.append("snapshot 包含 asOf 之后的数据，可能存在未来数据泄漏")
        if as_of > captured:
            problems.append("snapshot asOf 晚于 capturedAt")
        if captured > (now or datetime.now(UTC)) + timedelta(minutes=5):
            problems.append("snapshot capturedAt 位于允许时钟偏差之外的未来")
    count = observation.get("count")
    if type(count) is not int or not 1 <= count <= 9007199254740991:
        problems.append("snapshot observation.count 必须是正整数")
    return {"passed": not problems, "problems": problems}


def prompt_hash(value):
    normalized = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", js_string(value) if value is not None else "").strip(),
    ).lower()
    return hashlib.sha256(normalized.encode()).hexdigest()


def attest_registry(registry, public_cases, external=None):
    external = external or {}
    problems = []
    definitions = registry.get("datasets") if isinstance(registry.get("datasets"), list) else []
    if type(registry.get("schemaVersion")) is not int or registry["schemaVersion"] != 1:
        problems.append("dataset registry schemaVersion 必须为 1")
    if not registry.get("snapshotManifest"):
        problems.append("dataset registry 缺少 snapshotManifest")
    ids = [item.get("id") for item in definitions]
    if len(set(ids)) != len(ids):
        problems.append("dataset registry 包含重复 dataset id")
    for item in definitions:
        identity = item.get("id", "")
        if not identity or not item.get("version"):
            problems.append("dataset 缺少 id/version")
        if item.get("visibility") not in ("public", "hidden", "production_replay"):
            problems.append(identity + " visibility 无效")
        if item.get("split") not in ("smoke", "regression", "holdout", "shadow"):
            problems.append(identity + " split 无效")
        if item.get("visibility") == "public":
            if not item.get("path") or item.get("pathEnv"):
                problems.append(identity + " public dataset 必须声明 path")
        elif not item.get("pathEnv") or item.get("path"):
            problems.append(identity + " 非公开 dataset 只能通过 pathEnv 注入，禁止仓库内明文 path")
    public_ids, hashes = set(), set()
    for item in public_cases:
        identity = item.get("id") if isinstance(item.get("id"), str) else ""
        question = item.get("question") if isinstance(item.get("question"), str) else ""
        if not identity or not question:
            problems.append("public dataset 包含缺少 id/question 的 case")
        if identity in public_ids:
            problems.append("public dataset case id 重复：" + identity)
        public_ids.add(identity)
        hashes.add(prompt_hash(question))
    for visibility in ("hidden", "production_replay"):
        known = set()
        for item in external.get(visibility, []):
            identity = item.get("id") if isinstance(item.get("id"), str) else ""
            question = item.get("question") if isinstance(item.get("question"), str) else ""
            if not identity or not question:
                problems.append(visibility + " dataset 包含缺少 id/question 的 case")
            if identity in public_ids or identity in known:
                problems.append(f"{visibility} case id 污染或重复：{identity}")
            known.add(identity)
            if prompt_hash(question) in hashes:
                problems.append(f"{visibility} prompt 与 public dataset 重叠：{identity}")
    return {
        "passed": not problems,
        "problems": problems,
        "summary": {
            "publicCaseCount": len(public_cases),
            "hiddenCaseCount": len(external.get("hidden", [])),
            "productionReplayCaseCount": len(external.get("production_replay", [])),
            "publicPromptHashCount": len(hashes),
        },
    }
