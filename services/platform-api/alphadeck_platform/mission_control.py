import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .config import ROOT
from .evidence import verify
from .lease_session import LeaseSession
from .messages import js_length
from .mission import compile_mission
from .mission_store import MissionError, Missions
from .prefetch_market import timestamp
from .provenance import workspace_snapshot
from .resource_lock import resource_lock


def finance_definition(max_repair_attempts, expected_artifacts=()):
    catalog = json.loads((ROOT / "config/finance-mission-definition.json").read_text())
    if catalog.get("schemaVersion") != 1:
        raise ValueError("Unsupported finance Mission definition catalogue")
    definition = deepcopy(catalog["definition"])
    artifacts = {value["path"]: value for value in definition["artifacts"]}
    # Expected artifacts precede derived/control/optional surfaces in the original contract.
    subject_count = next(
        index for index, value in enumerate(definition["artifacts"]) if value["role"] != "subject"
    )
    ordered = {value["path"]: value for value in definition["artifacts"][:subject_count]}
    for path in expected_artifacts:
        known = artifacts.get(path)
        if known and known["role"] != "subject":
            value = {**known, "required": True}
        else:
            value = {"path": path, "role": "subject", "mutability": "frozen", "required": True}
        ordered[path] = value
    for path, value in artifacts.items():
        if path not in ordered:
            ordered[path] = value
    definition["artifacts"] = list(ordered.values())
    for node in definition["nodes"]:
        if node["key"] in {"workspace_generation", "validation", "evidence_verification"}:
            node["budget"]["maxAttempts"] = 1 + max_repair_attempts
    return definition


def artifact_paths(value):
    if not isinstance(value, (list, tuple)):
        return []
    return sorted(
        {
            item
            for item in value
            if isinstance(item, str)
            and 0 < js_length(item) <= 1024
            and not item.startswith("/")
            and ".." not in item.split("/")
        },
        key=lambda item: item.encode("utf-16-be", "surrogatepass"),
    )


def capture_candidate(
    root,
    *,
    source,
    source_request_id,
    source_run_id=None,
    summary=None,
    declared_artifacts=(),
    verified_artifacts=(),
    submitted_at=None,
):
    return {
        "schemaVersion": 1,
        "source": source,
        "sourceRunId": source_run_id,
        "sourceRequestId": source_request_id,
        "workspaceSha256": "sha256:" + workspace_snapshot(root)["sha256"],
        "summarySha256": "sha256:"
        + hashlib.sha256(((summary or "").strip() or source).encode()).hexdigest(),
        "declaredArtifacts": artifact_paths(declared_artifacts),
        "verifiedArtifacts": artifact_paths(verified_artifacts),
        "submittedAt": submitted_at or timestamp(),
    }


def candidate_from_run(root, request, result):
    if result.get("status") != "completed" or not result.get("terminalResult", {}).get("ok"):
        raise ValueError("Only a successful terminal submission can produce a candidate")
    terminal = result["terminalResult"]
    data = terminal.get("data") if isinstance(terminal.get("data"), dict) else {}
    return capture_candidate(
        root,
        source="pi_agent_submit_result",
        source_request_id=request or result["runId"],
        source_run_id=result["runId"],
        summary=data.get("summary")
        if isinstance(data.get("summary"), str)
        else terminal.get("content") or "PI Agent candidate submitted",
        declared_artifacts=data.get("artifacts"),
        verified_artifacts=data.get("verifiedArtifacts"),
        submitted_at=datetime.fromtimestamp(result["finishedAt"] / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
    )


class MissionControl:
    def __init__(self, database):
        self.store = Missions(database)

    async def create(self, project, request, objective, plan, max_repairs):
        composition = plan["composition"]
        spec = compile_mission(
            {
                "projectId": project,
                "requestId": request,
                "objective": objective,
                "capabilityId": plan.get("requestedCapabilityId") or plan["capabilityId"],
                "runPlanId": plan["runId"],
                "composition": {
                    "profileId": composition["profile"]["id"],
                    "profileVersion": composition["profile"]["version"],
                    "domainPacks": composition["domainPacks"],
                    "deliveryPackId": composition["deliveryPack"]["id"],
                    "deliveryPackVersion": composition["deliveryPack"]["version"],
                    "compositionSha256": composition["sha256"],
                },
                "entities": [
                    {"entityType": "finance.security", "canonicalId": symbol}
                    for symbol in plan["symbols"]
                ],
                "maxRepairAttempts": max_repairs,
                "definition": finance_definition(max_repairs, plan["expectedArtifacts"]),
                "createdAt": plan["createdAt"],
            }
        )
        return await self.store.ensure(spec)

    async def load(self, project, request, *, mission_id=None, generation_id=None):
        mission = await self.store.read(project, request)
        if not mission:
            raise ValueError("The durable Mission does not exist.")
        if mission_id and mission_id != mission["id"]:
            raise ValueError("The durable Mission ID does not match the dispatch envelope.")
        if generation_id and generation_id != mission["generationId"]:
            raise ValueError("The durable generation ID does not match the dispatch envelope.")
        return mission

    def verification(self, mission, root, *, owner=None, ttl_ms=60000, heartbeat_ms=15000):
        return VerificationSession(
            self.store, mission, root, owner=owner, ttl_ms=ttl_ms, heartbeat_ms=heartbeat_ms
        )


class VerificationSession:
    def __init__(self, store, mission, root, *, owner=None, ttl_ms=60000, heartbeat_ms=15000):
        self.store, self.mission, self.root = store, mission, Path(root).resolve(strict=True)
        self.owner, self.ttl, self.interval = (
            owner or "mission-verifier:" + str(uuid4()),
            ttl_ms,
            heartbeat_ms,
        )
        self.claim = self.lease = None
        if (
            type(ttl_ms) is not int
            or type(heartbeat_ms) is not int
            or not 0 < heartbeat_ms < ttl_ms
        ):
            raise ValueError("Invalid Mission verification heartbeat configuration")

    async def __aenter__(self):
        self.claim = await self.store.begin_verification(self.mission, self.owner, self.ttl)
        self.mission = self.claim["mission"]
        self.lease = LeaseSession(
            lambda: self.store.heartbeat(self.mission, self.claim, self.ttl),
            ttl_ms=self.ttl,
            interval_ms=self.interval,
        )
        await self.lease.__aenter__()
        return self

    async def run(self, operation):
        if self.lease is None:
            raise ValueError("Mission verification session has not been claimed")
        return await self.lease.run(operation)

    async def verify(self, preview, *, client=None):
        async def operation():
            self.lease.healthy()
            current = await self.store.read(self.mission["projectId"], self.mission["requestId"])
            if (
                not current
                or current["id"] != self.mission["id"]
                or current["status"] != "verifying"
                or current["candidateVersion"] != self.mission["candidateVersion"]
            ):
                raise MissionError(
                    "MISSION_VERIFICATION_LEASE_LOST",
                    "Evidence verification requires the current exclusively claimed candidate.",
                )
            spec = await self.store.spec(current)
            async with resource_lock(
                self.root,
                {
                    "purpose": "mission_evidence_verification",
                    "missionId": current["id"],
                    "projectId": current["projectId"],
                    "requestId": current["requestId"],
                },
                recover_dead_local_owner=True,
            ):
                decision = await verify(
                    {
                        "missionId": current["id"],
                        "generationId": current["generationId"],
                        "candidateVersion": current["candidateVersion"],
                        "missionSpec": spec,
                        "missionSpecSha256": current["specHash"],
                        "workspaceRoot": self.root,
                        "preview": preview,
                    },
                    client=client,
                )
                self.lease.healthy()
                recorded = await self.store.evidence(current, self.claim, decision)
                self.mission = recorded["mission"]
                return {**recorded, "decision": decision}

        return await self.run(operation)

    async def __aexit__(self, *error):
        if self.lease:
            await self.lease.__aexit__(*error)
        if self.claim:
            current = await self.store.read(self.mission["projectId"], self.mission["requestId"])
            if current and current["status"] == "verifying":
                try:
                    self.mission = await self.store.abandon(self.mission, self.claim)
                except MissionError as cause:
                    if cause.code != "MISSION_VERIFICATION_LEASE_LOST":
                        raise
