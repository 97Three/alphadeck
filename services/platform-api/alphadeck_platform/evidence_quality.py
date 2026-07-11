import math
import re

from .data_quality import timestamp
from .finance_values import coalesce, js_string, record


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def assess_evidence(sources_value, quality_value):
    failures, warnings, paths, identities = [], [], [], set()
    sources = (record(sources_value) or {}).get("sources")
    if not isinstance(sources, list) or not sources or len(sources) > 1000:
        failures.append("sources_invalid")
    else:
        for index, value in enumerate(sources):
            source, prefix = record(value) or {}, f"sources[{index}]"
            if not nonempty(coalesce(source.get("source"), source.get("provider"))):
                failures.append(prefix + ":provider_missing")
            if not nonempty(source.get("endpoint")) and not nonempty(source.get("artifact_path")):
                failures.append(prefix + ":reference_missing")
            identity = source.get("id")
            if nonempty(identity):
                if identity in identities:
                    failures.append(prefix + ":duplicate_source_id")
                identities.add(identity)
            rows = source.get("row_count")
            if "row_count" in source and (
                type(rows) not in {int, float}
                or not math.isfinite(rows)
                or rows < 0
                or rows > 9007199254740991
                or rows != int(rows)
            ):
                failures.append(prefix + ":row_count_invalid")
            if "artifact_path" in source:
                path = source["artifact_path"]
                if (
                    not nonempty(path)
                    or not re.match(r"(?:data_file|evidence)/", path)
                    or re.search(r"[\\\x00:#]", path)
                    or any(part in {"", ".", ".."} for part in path.split("/"))
                ):
                    failures.append(prefix + ":artifact_path_invalid")
                elif path not in paths:
                    paths.append(path)
            observed = coalesce(source.get("fetched_at"), source.get("as_of"))
            if observed is None or observed == "":
                warnings.append(prefix + ":observation_time_missing")
            elif timestamp(observed) is None:
                failures.append(prefix + ":observation_time_invalid")
    quality = record(quality_value) or {}
    if js_string(quality.get("status")) not in {"ok", "warning", "error"}:
        failures.append("quality_status_invalid")
    if quality.get("status") == "error":
        failures.append("quality_error")
    for index, item in enumerate(
        quality["datasets"] if isinstance(quality.get("datasets"), list) else []
    ):
        item = record(item) or {}
        if item.get("status") == "error" and item.get("critical") is not False:
            failures.append(f"datasets[{index}]:critical_error")
        elif item.get("status") in {"warning", "error"}:
            warnings.append(f"datasets[{index}]:quality_warning")
    return {
        "passed": not failures,
        "failures": failures[:32],
        "warnings": warnings[:32],
        "artifactPaths": paths,
    }
