"""List missing runtime contracts. The release gate fails until migration is complete."""

import argparse
import hashlib
import json
import re

from .config import ROOT


def fastapi_path(path):
    path = re.sub(r"\[\.\.\.(\w+)\]", r"{\1}", path)
    return re.sub(r"\[(\w+)\]", r"{\1}", path)


def report(app):
    manifest = json.loads(
        (ROOT / "config/python-migration-contracts.json").read_text(encoding="utf-8")
    )
    verbs = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    implemented = {
        (path, method.upper())
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method.upper() in verbs
    }
    missing = []
    implemented_count = 0
    drifted = []
    partial = []
    expected_by_path = {}
    for contract in manifest["contracts"]:
        path = fastapi_path(contract["path"])
        expected_by_path[path] = set(contract["methods"])
        present = {method for method in contract["methods"] if (path, method) in implemented}
        if present and present != set(contract["methods"]):
            partial.append(contract["path"])
        source = ROOT / "migration-reference/typescript-backend" / contract["source"]
        if not source.is_file():
            source = ROOT / contract["source"]
        if (
            not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest() != contract["sha256"]
        ):
            drifted.append(contract["source"])
        for method in contract["methods"]:
            if (path, method) in implemented:
                implemented_count += 1
            else:
                missing.append(
                    {"path": contract["path"], "method": method, "source": contract["source"]}
                )
    runtime = json.loads((ROOT / "config/python-migration-runtime.json").read_text())
    pending_workers = runtime["pendingWorkers"]
    pending_runtime = runtime["pending"]
    forwarded = json.loads((ROOT / "config/python-backend-routes.json").read_text())
    routing_problems = []
    for source in forwarded:
        path = re.sub(r":(\w+)\*?", r"{\1}", source)
        expected = expected_by_path.get(path)
        if expected is None or any((path, method) not in implemented for method in expected):
            routing_problems.append(source)
    return {
        "complete": not missing
        and not pending_workers
        and not pending_runtime
        and not drifted
        and not routing_problems,
        "routeFiles": len(manifest["contracts"]),
        "implementedMethodContracts": implemented_count,
        "pendingMethodContracts": len(missing),
        "missing": missing,
        "pendingWorkers": pending_workers,
        "pendingRuntimeComponents": pending_runtime,
        "sourceDrift": drifted,
        "forwardedPaths": forwarded,
        "routingProblems": routing_problems,
        "partiallyImplementedPaths": partial,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    from app import app

    result = report(app)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.require_complete and not result["complete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
