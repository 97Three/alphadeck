import json
import re
from copy import deepcopy
from uuid import uuid4

from .config import ROOT
from .evaluation_oracles import OPERATORS, TARGETS, pattern
from .evaluation_queue import strings
from .evaluation_scoring import number
from .provenance import js_json
from .resource_lock import resource_lock, sync_directory
from .scaffold import controlled_path
from .skill_integrity import read_regular
from .workspace import write_atomic

CASES_PATH = "benchmarks/alphadeck/cases.json"
SETS_PATH = "benchmarks/alphadeck/eval-sets.json"


def text(value, default=""):
    return value if isinstance(value, str) else default


def truthy(value):
    return True if isinstance(value, (dict, list)) else bool(value)


def case_type(value):
    return text(value.get("type")) or (
        "clarification_required"
        if value.get("expectClarification") is True
        else "generated_project"
    )


def coverage(value):
    level = value.get("coverageLevel")
    return (
        level
        if level in ("routing", "contract", "live_e2e", "production")
        else "routing"
        if case_type(value) == "renderer_capability_contract"
        else "contract"
    )


def tags(value):
    result = {}

    def add(tag):
        result[tag] = None

    for key, prefix in (
        ("capabilityId", ""),
        ("expectedAssetType", "asset:"),
        ("expectedTemplateId", "template:"),
        ("expectedVariantId", "variant:"),
    ):
        if key == "expectedAssetType":
            add(case_type(value))
        if text(value.get(key)):
            add(prefix + value[key])
    if value.get("expectClarification") is True:
        add("intent:clarification_required")
    if value.get("expectClarification") is False:
        add("intent:no_false_clarification")
    if truthy(value.get("imageAttachment")):
        add("input:image_attachment")
    if value.get("visualCheck") is True:
        add("visual:playwright")
    if value.get("expectedImageExtraction") is True:
        add("evidence:image_extraction")
    special = {
        "clarification_continuation": "intent:clarification_continuation",
        "repair_plan": "validation:repair_plan",
        "source_degradation_contract": "data:source_degradation",
        "runtime_registry": "runtime:deepseek_v4_flash",
        "renderer_capability_contract": "dashboard:renderer_capability",
    }
    if case_type(value) in special:
        add(special[case_type(value)])
    add("coverage:" + coverage(value))
    if value.get("productionSupported") is True:
        add("coverage:production")
    for item in strings(value.get("safetyTags")):
        add("safety:" + item)
    expectations = value.get("selectionExpectations")
    for item in expectations if isinstance(expectations, list) else []:
        if isinstance(item, dict):
            for key, prefix in (
                ("capabilityId", ""),
                ("expectedTemplateId", "template:"),
                ("expectedVariantId", "variant:"),
            ):
                if text(item.get(key)):
                    add(prefix + item[key])
    if len(strings(value.get("expectedSymbols"))) > 1:
        add("data:multi_symbol")
    return list(result)


def normalize_case(value, catalog=None):
    catalog = catalog or json.loads((ROOT / "config/evaluation-catalog.json").read_text())
    capability, type_value = text(value.get("capabilityId"), "unknown"), case_type(value)
    return {
        "id": text(value.get("id"), "unknown"),
        "name": text(value.get("name"), text(value.get("id"), "未命名用例")),
        "question": text(value.get("question")),
        "capabilityId": capability,
        "capabilityLabel": catalog["capabilityLabels"].get(capability, capability),
        "type": type_value,
        "typeLabel": catalog["typeLabels"].get(type_value, type_value),
        "expectedSymbols": list(
            dict.fromkeys(
                filter(
                    None,
                    [*strings(value.get("expectedSymbols")), text(value.get("expectedSymbol"))],
                )
            )
        ),
        **{
            key: text(value.get(key)) or None
            for key in ("expectedAssetType", "expectedTemplateId", "expectedVariantId")
        },
        **{
            key: strings(value.get(key))
            for key in ("expectedDatasets", "expectedRawFiles", "expectedFinalFields", "safetyTags")
        },
        "tags": tags(value),
        "coverageLevel": coverage(value),
        "productionSupported": value.get("productionSupported") is True,
        "oracleAssertions": [
            item for item in value.get("oracleAssertions", []) if isinstance(item, dict)
        ]
        if isinstance(value.get("oracleAssertions"), list)
        else [],
        "hasImageAttachment": truthy(value.get("imageAttachment")),
        "expectClarification": value.get("expectClarification") is True,
        "visualCheck": value.get("visualCheck") is True,
    }


def string_list(value):
    values = (
        strings(value)
        if isinstance(value, list)
        else re.split(r"[\n,，]", value)
        if isinstance(value, str)
        else []
    )
    return [item.strip() for item in values if item.strip()]


def validate_oracles(value):
    if not isinstance(value, list):
        raise ValueError("oracleAssertions 必须是数组。")
    ids = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 条 oracle 必须是对象。")
        identity = item.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_:-]+", identity):
            raise ValueError(f"第 {index + 1} 条 oracle 缺少有效 id。")
        if identity in ids:
            raise ValueError("oracle id 重复：" + identity)
        ids.add(identity)
        for key, allowed in (("target", TARGETS), ("operator", OPERATORS)):
            if not isinstance(item.get(key), str) or item[key] not in allowed:
                raise ValueError(f"{identity} 的 {key} 无效。")
        if "path" in item and not isinstance(item["path"], str):
            raise ValueError(identity + " 的 path 必须是字符串。")
        if "severity" in item and item["severity"] not in ("error", "warning"):
            raise ValueError(identity + " 的 severity 必须是 error 或 warning。")
        if item["operator"] == "between" and (
            number(item.get("min"), None) is None
            or number(item.get("max"), None) is None
            or item["min"] > item["max"]
        ):
            raise ValueError(identity + " 的 between 必须声明有效 min/max。")
        if item["operator"] not in {"exists", "not_exists", "between"} and "value" not in item:
            raise ValueError(f"{identity} 的 {item['operator']} 必须声明 value。")
        if "tolerance" in item and (
            number(item["tolerance"], None) is None or item["tolerance"] < 0
        ):
            raise ValueError(identity + " 的 tolerance 必须是非负数。")
        if item["operator"] in {"matches", "not_matches"} and pattern(item["value"]) is None:
            raise ValueError(identity + " 的正则表达式无效。")
    return deepcopy(value)


def slug(value, prefix):
    value = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")[:48]
    return prefix + "-" + (value or uuid4().hex)


class EvaluationCases:
    def __init__(self, root=ROOT):
        self.root = root.resolve(strict=True)
        self.catalog = json.loads((ROOT / "config/evaluation-catalog.json").read_text())

    def raw(self, relative=CASES_PATH):
        try:
            value = json.loads(read_regular(controlled_path(self.root, relative), 8388608))
            return (
                [item for item in value if isinstance(item, dict)]
                if isinstance(value, list)
                else []
            )
        except (OSError, ValueError):
            return []

    def list(self):
        return [normalize_case(item, self.catalog) for item in self.raw()]

    def normalize_set(self, value):
        return {
            "id": text(value.get("id"), "custom-set-" + uuid4().hex),
            "name": text(value.get("name"), "未命名评测集"),
            "description": text(value.get("description")),
            "category": text(value.get("category"), "自定义"),
            "caseIds": strings(value.get("caseIds")),
            "custom": True,
        }

    def sets(self):
        known = {item["id"] for item in self.list()}
        result = []
        for value in self.raw(SETS_PATH):
            value = self.normalize_set(value)
            value["caseIds"] = [identity for identity in value["caseIds"] if identity in known]
            if value["caseIds"]:
                result.append(value)
        return result

    def write(self, relative, value):
        controlled_path(self.root, relative)
        target = write_atomic(self.root, relative, (js_json(value, indent=2) + "\n").encode())
        sync_directory(target.parent)

    async def create(self, value):
        async with resource_lock(
            self.root, {"purpose": "evaluation_case_edit"}, recover_dead_local_owner=True
        ):
            cases = self.raw()
            name, question = text(value.get("name")).strip(), text(value.get("question")).strip()
            capability, kind = (
                text(value.get("capabilityId"), "asset_comparison").strip(),
                text(value.get("type"), "generated_project").strip(),
            )
            identity = text(value.get("id")).strip() or slug(name or capability, "case")
            if not re.fullmatch(r"[A-Za-z0-9_:-]+", identity):
                raise ValueError("用例 ID 只能包含字母、数字、下划线、短横线和冒号。")
            if any(text(item.get("id")) == identity for item in cases):
                raise ValueError("用例 ID 已存在：" + identity)
            if not name:
                raise ValueError("请填写用例名称。")
            if not question:
                raise ValueError("请填写用户 Query。")
            record = {
                "id": identity,
                "name": name,
                "question": question,
                "capabilityId": capability,
                "type": kind,
            }
            symbols = string_list(value.get("expectedSymbols"))
            if symbols:
                record["expectedSymbol" if len(symbols) == 1 else "expectedSymbols"] = (
                    symbols[0] if len(symbols) == 1 else symbols
                )
            for key in (
                "expectedAssetType",
                "expectedTemplateId",
                "expectedVariantId",
                "coverageLevel",
            ):
                if value.get(key):
                    record[key] = value[key]
            for key in (
                "expectedDatasets",
                "expectedRawFiles",
                "expectedFinalFields",
                "safetyTags",
            ):
                if items := string_list(value.get(key)):
                    record[key] = items
            assertions = (
                validate_oracles(value["oracleAssertions"]) if "oracleAssertions" in value else []
            )
            if value.get("productionSupported") and not assertions:
                raise ValueError("产品支持用例必须至少提供一条事实或安全 oracle。")
            if value.get("productionSupported") and not record.get("safetyTags"):
                raise ValueError("产品支持用例必须至少提供一个安全标签。")
            if assertions:
                record["oracleAssertions"] = assertions
            for key in ("productionSupported", "expectClarification", "visualCheck"):
                if value.get(key):
                    record[key] = True
            self.write(CASES_PATH, [*cases, record])
            return normalize_case(record, self.catalog)

    async def create_set(self, value):
        async with resource_lock(
            self.root, {"purpose": "evaluation_set_edit"}, recover_dead_local_owner=True
        ):
            sets, cases = self.raw(SETS_PATH), self.list()
            known = {item["id"] for item in cases}
            selected = [
                identity for identity in string_list(value.get("caseIds")) if identity in known
            ]
            reserved = {
                "all",
                *("capability:" + item["capabilityId"] for item in cases),
                *("type:" + item["type"] for item in cases),
            }
            if any(item["visualCheck"] or item["hasImageAttachment"] for item in cases):
                reserved.add("special:visual")
            if any(
                item["expectClarification"] or "clarification" in item["type"] for item in cases
            ):
                reserved.add("special:clarification")
            name = text(value.get("name")).strip()
            identity = text(value.get("id")).strip() or slug(name or "eval-set", "custom")
            if not re.fullmatch(r"[A-Za-z0-9_:-]+", identity):
                raise ValueError("评测集 ID 只能包含字母、数字、下划线、短横线和冒号。")
            if identity in reserved or any(text(item.get("id")) == identity for item in sets):
                raise ValueError("评测集 ID 已存在：" + identity)
            if not name:
                raise ValueError("请填写评测集名称。")
            if not selected:
                raise ValueError("请至少选择一个测试用例。")
            record = {
                "id": identity,
                "name": name,
                "description": text(value.get("description")),
                "category": text(value.get("category"), "自定义"),
                "caseIds": selected,
            }
            self.write(SETS_PATH, [*sets, record])
            return self.normalize_set(record)
