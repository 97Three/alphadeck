import asyncio
import json
import re
import unicodedata
from copy import deepcopy

from .config import ROOT
from .dashboard_prerequisites import contract_string, satisfied
from .data_identity import assess_identity
from .file_tools import record as input_record
from .file_tools import write_batch
from .finance_values import coalesce, record
from .messages import js_length
from .skill_integrity import read_regular
from .tool_paths import ToolError

CATALOG = json.loads((ROOT / "config/dashboard-spec-catalog.json").read_text())
CAPABILITIES = {(item["templateId"], item["variantId"]): item for item in CATALOG["capabilities"]}
TEMPLATES = {item["templateId"] for item in CATALOG["capabilities"]}
PLAN = ".data-agent/finance-run-plan.json"
FINAL = "data_file/final/dashboard-data.json"


def supported(template, variant):
    return bool(variant and CAPABILITIES.get((template, variant), {}).get("supported"))


def normalized_component(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip()).lower()


def components(value, label):
    if not isinstance(value, list) or not value:
        raise ToolError(
            "DASHBOARD_SPEC_CONTRACT_INCOMPLETE",
            label + " must declare at least one dashboard component.",
        )
    if len(value) > 64:
        raise ToolError(
            "DASHBOARD_SPEC_CONTRACT_INVALID", label + " exceeds the 64-component contract limit."
        )
    normalized = {}
    for item in value:
        component = contract_string(item)
        if component is None:
            raise ToolError(
                "DASHBOARD_SPEC_CONTRACT_INVALID",
                label + " contains an invalid dashboard component.",
            )
        key = normalized_component(component)
        if key in normalized:
            raise ToolError(
                "DASHBOARD_SPEC_CONTRACT_INVALID",
                label + " contains duplicate dashboard components after normalization.",
                {"component": component},
            )
        normalized[key] = component
    return normalized


def authoritative(label, planned, prepared):
    if not planned or not prepared:
        raise ToolError(
            "DASHBOARD_SPEC_CONTRACT_INCOMPLETE",
            label + " must be declared by both the run plan and final data.",
            {"planned": planned, "prepared": prepared},
        )
    if planned != prepared:
        raise ToolError(
            "DASHBOARD_SPEC_CONTRACT_MISMATCH",
            label + " differs between the run plan and final data.",
            {"planned": planned, "prepared": prepared},
        )
    return planned


def compile_dashboard_spec(plan, final, assertion=None):
    assertion = assertion or {}
    if contract_string(plan.get("status")) != "planned":
        raise ToolError(
            "DASHBOARD_SPEC_PLAN_NOT_READY",
            "The authoritative run plan must have status=planned before dashboard compilation.",
            {"status": plan.get("status")},
        )
    planned, prepared = record(plan.get("visualization")), record(final.get("visualization"))
    if planned is None or planned.get("required") is not True:
        raise ToolError(
            "DASHBOARD_SPEC_VISUALIZATION_NOT_REQUIRED",
            "The authoritative run plan must explicitly declare visualization.required=true.",
        )
    if prepared is None:
        raise ToolError(
            "DASHBOARD_SPEC_CONTRACT_INCOMPLETE",
            "Final dashboard data must contain a visualization contract.",
        )
    template = authoritative(
        "templateId",
        contract_string(coalesce(planned.get("templateId"), planned.get("template_id"))),
        contract_string(coalesce(prepared.get("template_id"), prepared.get("templateId"))),
    )
    variant = authoritative(
        "variantId",
        contract_string(coalesce(planned.get("variantId"), planned.get("variant_id"))),
        contract_string(coalesce(prepared.get("variant_id"), prepared.get("variantId"))),
    )
    for key, value in (("templateId", template), ("variantId", variant)):
        if assertion.get(key) and assertion[key] != value:
            raise ToolError(
                "DASHBOARD_SPEC_ASSERTION_FAILED",
                f"Requested {key} {assertion[key]} does not match authoritative {value}.",
            )
    if template not in TEMPLATES:
        raise ToolError(
            "DASHBOARD_SPEC_TEMPLATE_UNSUPPORTED",
            f"No trusted AlphaDeck renderer is registered for template {template}.",
            {"templateId": template, "variantId": variant},
        )
    capability = CAPABILITIES.get((template, variant))
    if capability is None:
        raise ToolError(
            "DASHBOARD_SPEC_VARIANT_UNSUPPORTED",
            f"No trusted AlphaDeck renderer capability is registered for {template}/{variant}.",
            {"templateId": template, "variantId": variant},
        )
    if not capability["supported"]:
        raise ToolError(
            "DASHBOARD_SPEC_VARIANT_UNSUPPORTED",
            f"Dashboard variant {template}/{variant} is not implemented by a trusted renderer.",
            {"templateId": template, "variantId": variant, "reason": capability["reason"]},
        )
    identity = assess_identity(plan, final)
    if not identity["ready"]:
        raise ToolError(
            "DASHBOARD_SPEC_DATA_IDENTITY_MISMATCH",
            "Final dashboard data is not bound to the authoritative run and symbol universe.",
            {"reasons": identity["reasons"]},
        )
    plan_components = components(planned.get("panels"), "run_plan.visualization.panels")
    final_components = components(
        prepared.get("required_components"), "final.visualization.required_components"
    )

    def difference(left, right):
        return [value for key, value in left.items() if key not in right]

    if plan_components.keys() != final_components.keys():
        raise ToolError(
            "DASHBOARD_SPEC_COMPONENTS_MISMATCH",
            "Dashboard components differ between the run plan and final data.",
            {
                "planOnly": difference(plan_components, final_components),
                "finalOnly": difference(final_components, plan_components),
            },
        )
    implemented = components(capability["requiredComponents"], "renderer.requiredComponents")
    if final_components.keys() != implemented.keys():
        raise ToolError(
            "DASHBOARD_SPEC_COMPONENT_UNSUPPORTED",
            f"The trusted renderer does not exactly implement the authoritative component contract for {template}/{variant}.",
            {
                "unsupported": difference(final_components, implemented),
                "missing": difference(implemented, final_components),
            },
        )
    missing = prepared.get("missing_components")
    if "missing_components" in prepared and not isinstance(missing, list):
        raise ToolError(
            "DASHBOARD_SPEC_CONTRACT_INVALID",
            "final.visualization.missing_components must be an array when declared.",
        )
    if missing:
        raise ToolError(
            "DASHBOARD_SPEC_COMPONENT_UNSUPPORTED",
            "Final dashboard data declares unresolved visualization components.",
            {"missingComponents": missing[:64]},
        )
    absent = []
    for requirement in capability["dataPrerequisites"]:
        try:
            ready = satisfied(requirement["predicate"], final)
        except (ValueError, TypeError, KeyError):
            ready = False
        if not ready:
            absent.append({key: requirement[key] for key in ("id", "description")})
    if absent:
        raise ToolError(
            "DASHBOARD_SPEC_DATA_PREREQUISITE_FAILED",
            f"Final dashboard data cannot satisfy {template}/{variant}.",
            {"missing": absent},
        )
    return {
        "schemaVersion": 1,
        "templateId": template,
        "variantId": variant,
        "renderer": capability["renderer"],
        "requiredComponents": deepcopy(capability["requiredComponents"]),
        "dataPrerequisites": [item["id"] for item in capability["dataPrerequisites"]],
        "dataArtifact": FINAL,
        "outputArtifacts": ["src/App.vue", "src/style.css"],
    }


def assess_readiness(plan, final):
    try:
        return {
            "ready": True,
            "errorCode": None,
            "reasons": [],
            "spec": compile_dashboard_spec(plan, final),
        }
    except ToolError as cause:
        details = cause.details if isinstance(cause.details, dict) else {}
        reasons = [value for value in details.get("reasons", []) if isinstance(value, str)]
        missing = [
            identity
            for value in details.get("missing", [])
            if isinstance(value, dict) and (identity := contract_string(value.get("id")))
        ]
        return {
            "ready": False,
            "errorCode": cause.code,
            "reasons": reasons or missing or [cause.code],
            "spec": None,
        }


def parse_assertion(value):
    value = input_record(value)
    result = {}
    for key in ("templateId", "variantId"):
        if key not in value:
            continue
        item = value[key]
        if not isinstance(item, str) or not item:
            raise ToolError("INVALID_TOOL_INPUT", key + " must be a non-empty string.")
        if js_length(item) > 160:
            raise ToolError("INVALID_TOOL_INPUT", key + " exceeds 160 characters.")
        result[key] = item
    return result


class DashboardSpecTool:
    def __init__(self, policy, *, timeout_ms=15000, max_file_bytes=256000, max_total_bytes=512000):
        self.policy, self.timeout, self.max_file, self.max_total = (
            policy,
            timeout_ms,
            max_file_bytes,
            max_total_bytes,
        )
        self.definition = deepcopy(CATALOG["definition"])

    def read_contract(self, relative):
        path = self.policy.read(relative).canonical
        if not path.is_file():
            raise ToolError(
                "DASHBOARD_SPEC_CONTRACT_INVALID", relative + " must be a regular JSON file."
            )
        try:
            value = json.loads(read_regular(path, 2000000))
        except (ValueError, OSError) as cause:
            raise ToolError(
                "DASHBOARD_SPEC_CONTRACT_INVALID",
                relative + " must be a bounded, valid JSON object.",
            ) from cause
        if not isinstance(value, dict):
            raise ToolError("DASHBOARD_SPEC_CONTRACT_INVALID", relative + " must be a JSON object.")
        return value

    async def execute(self, value, context):
        try:
            async with asyncio.timeout(self.timeout / 1000):
                spec = compile_dashboard_spec(
                    self.read_contract(PLAN), self.read_contract(FINAL), parse_assertion(value)
                )
                rendered = CATALOG["renders"][spec["renderer"]]
                result = await write_batch(
                    self.policy,
                    [
                        {"path": path, "content": rendered[key].encode()}
                        for path, key in (("src/App.vue", "page"), ("src/style.css", "styles"))
                    ],
                    context,
                    max_file=self.max_file,
                    max_total=self.max_total,
                )
                return {
                    "ok": True,
                    "data": {"spec": spec, **result},
                    "content": f"Compiled {spec['templateId']}/{spec['variantId'] or 'default'} with the trusted {spec['renderer']} renderer into "
                    + ", ".join(spec["outputArtifacts"])
                    + ".",
                    "metadata": {
                        "dashboardSpecVersion": spec["schemaVersion"],
                        **{key: spec[key] for key in ("templateId", "variantId", "renderer")},
                    },
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
