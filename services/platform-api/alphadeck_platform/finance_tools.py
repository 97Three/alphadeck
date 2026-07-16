import hashlib
from copy import deepcopy

from .dashboard_inspector import DashboardInspector
from .dashboard_spec import DashboardSpecTool
from .file_tools import record
from .image_tool import ImageTool
from .provenance import js_json
from .quant_tool import QuantApiTool
from .semantic_edit import SemanticEditTool
from .structured_tools import StructuredFileTools

GENERIC_READS = {"list_files", "read_file", "read_file_range", "search_files"}
PREPARED_WRITES = {"src/App.vue", "src/style.css"}
CONTRACT_TARGETS = [
    ".data-agent/task.json",
    ".data-agent/plan.json",
    ".data-agent/finance-query-rewrite.json",
    ".data-agent/finance-run-plan.json",
    "data_file/final/dashboard-data.json",
    "evidence/sources.json",
    "evidence/data_quality.json",
    "src/App.vue",
    "src/style.css",
]


class FinanceTools:
    def __init__(
        self,
        root,
        *,
        profile="generation",
        profile_allowed_write_globs=None,
        allowed_write_globs=(),
        include_default=True,
        targeted_reads_only=False,
        prepared_surface=None,
        include_dashboard_spec=False,
        include_dashboard_inspector=True,
        include_semantic_edit=False,
        include_quant_api=True,
        include_image_extraction=True,
        research_time=None,
        market_client=None,
        allowed_mutation_tool_names=None,
        additional_tools=(),
        max_file=1000000,
        max_write=1000000,
        max_output=12000,
    ):
        if profile not in {"generation", "repair"}:
            raise ValueError("Unknown PI Agent tool profile")
        if profile == "repair" and profile_allowed_write_globs is None:
            raise ValueError(
                "PI Agent repair tools require a trusted failure-scoped write allowlist."
            )
        globs = [*(profile_allowed_write_globs or []), *allowed_write_globs]
        if prepared_surface and (include_quant_api or include_image_extraction or additional_tools):
            raise ValueError(
                "Finance prepared surfaces require quant API, image extraction, and plugin tools to be disabled."
            )
        if prepared_surface and any(glob not in PREPARED_WRITES for glob in globs):
            raise ValueError(
                "Finance prepared surfaces reject writes outside the certified app source scope."
            )
        if prepared_surface and include_default:
            raise ValueError(
                "PI Agent prepared surfaces require includeDefaultWriteGlobs=false and an explicit write scope."
            )
        if prepared_surface not in {None, "standard", "custom"}:
            raise ValueError("Unknown PI Agent prepared tool surface")
        if prepared_surface == "standard" and not include_dashboard_spec:
            raise ValueError(
                "PI Agent prepared standard surface requires a domain delivery compiler."
            )
        if prepared_surface == "custom" and not include_semantic_edit:
            raise ValueError("PI Agent prepared custom surface requires semantic_edit.")
        self.base = StructuredFileTools(
            root,
            finance=True,
            allowed_write_globs=globs,
            include_default=include_default,
            structured_json_globs=(
                "data/**/*.json",
                "data_file/final/**/*.json",
                "evidence/**/*.json",
            )
            if profile == "generation"
            else (),
            max_file=max_file,
            max_write=max_write,
            max_output=max_output,
        )
        self.policy = self.base.policy
        base = {definition["name"]: definition for definition in self.base.definitions}
        first_party = {}
        if include_dashboard_inspector and not prepared_surface:
            first_party["inspect_dashboard_contract"] = DashboardInspector(
                self.policy, max_output_chars=max_output
            )
        if include_dashboard_spec:
            first_party["apply_dashboard_spec"] = DashboardSpecTool(
                self.policy, max_file_bytes=min(max_write, 256000)
            )
        if include_semantic_edit:
            first_party["semantic_edit"] = SemanticEditTool(
                self.policy, max_file_bytes=max_file, max_write_bytes=max_write
            )
        if include_quant_api and not prepared_surface:
            first_party["quant_api_get"] = QuantApiTool(
                research_time=research_time, client=market_client, max_output_chars=max_output
            )
        if include_image_extraction and not prepared_surface:
            first_party["quant_extract_uploaded_image"] = ImageTool(
                self.policy, max_output_chars=max_output
            )
        if prepared_surface == "standard":
            names = ["apply_dashboard_spec", "submit_result"]
        elif prepared_surface == "custom":
            names = ["query_json", "query_text_file", "semantic_edit", "submit_result"]
        else:
            names = [
                name
                for name in base
                if name not in {"submit_result", "query_json", "query_text_file"}
                and not (targeted_reads_only and name in GENERIC_READS)
            ]
            names += [
                name
                for name in (
                    "inspect_dashboard_contract",
                    "query_json",
                    "query_text_file",
                    "apply_dashboard_spec",
                    "semantic_edit",
                    "quant_api_get",
                    "submit_result",
                    "quant_extract_uploaded_image",
                )
                if name in base or name in first_party
            ]
        definitions = [
            deepcopy(first_party[name].definition if name in first_party else base[name])
            for name in names
        ]
        self.extensions = {}
        for tool in additional_tools:
            definition = deepcopy(tool.definition)
            name = definition["name"]
            if name in names or name in self.extensions:
                raise ValueError("Duplicate PI Agent tool name: " + name)
            definition.pop("projectContextReceipt", None)
            definition.pop("approval", None)
            definition.setdefault("effect", "external_write")
            definition.setdefault("idempotency", "reconcile_required")
            definitions.append(definition)
            self.extensions[name] = tool
        mutators = {
            definition["name"]
            for definition in definitions
            if definition.get("effect", "external_write") in {"workspace_write", "external_write"}
        }
        if allowed_mutation_tool_names is not None:
            unknown = [
                name for name in dict.fromkeys(allowed_mutation_tool_names) if name not in mutators
            ]
            if unknown:
                raise ValueError(
                    "Unknown PI Agent mutation tool allowlist entries: " + ", ".join(unknown)
                )
            permitted = set(allowed_mutation_tool_names)
            definitions = [
                definition
                for definition in definitions
                if definition["name"] not in mutators or definition["name"] in permitted
            ]
        self.definitions = definitions
        self.registered = {definition["name"] for definition in definitions}
        self.first_party = first_party

    async def execute(self, name, input, context=None):
        if name not in self.registered:
            message = "Unknown Agent tool: " + name
            return {
                "ok": False,
                "error": {"code": "UNKNOWN_TOOL", "message": message},
                "content": message,
            }
        if name in self.first_party:
            return await self.first_party[name].execute(input, context)
        if name in self.extensions:
            return await self.extensions[name].execute(input, context)
        return await self.base.execute(name, input, context)

    def project_context_receipt(self, name, arguments, result):
        if name in self.extensions:
            return None
        arguments = record(arguments)
        if name == "inspect_dashboard_contract":
            return CONTRACT_TARGETS
        if name == "quant_api_get":
            return [arguments["path"]] if isinstance(arguments.get("path"), str) else []
        if name == "quant_extract_uploaded_image":
            data = result.get("data") if result.get("ok") else None
            images = data.get("images", []) if isinstance(data, dict) else []
            targets = [
                value["path"]
                for value in images[:32]
                if isinstance(value, dict) and isinstance(value.get("path"), str) and value["path"]
            ]
            return targets or (
                [arguments["path"]] if isinstance(arguments.get("path"), str) else []
            )
        if name == "apply_dashboard_spec":
            data = result.get("data") if result.get("ok") else None
            files = data.get("files", []) if isinstance(data, dict) else []
            files = files[:16] if isinstance(files, list) else []
            targets = [
                value["path"]
                for value in files
                if isinstance(value, dict) and isinstance(value.get("path"), str) and value["path"]
            ]
            entries = [
                {"path": value["path"], "afterSha256": value["afterSha256"]}
                for value in files
                if isinstance(value, dict)
                and isinstance(value.get("path"), str)
                and value["path"]
                and isinstance(value.get("afterSha256"), str)
                and value["afterSha256"]
            ]
            entries.sort(key=lambda value: value["path"])
            return {
                "targetReferences": targets or ["src/App.vue", "src/style.css"],
                **(
                    {"artifactSha256": hashlib.sha256(js_json(entries).encode()).hexdigest()}
                    if entries
                    else {}
                ),
            }
        if name == "semantic_edit":
            targets = [arguments["path"]] if isinstance(arguments.get("path"), str) else []
            data = result.get("data") if result.get("ok") else None
            return {
                "targetReferences": targets,
                **(
                    {"artifactSha256": data["afterSha256"]}
                    if isinstance(data, dict) and data.get("afterSha256")
                    else {}
                ),
            }
        return self.base.project_context_receipt(name, arguments, result)
