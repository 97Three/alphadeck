import re
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from .generation_lease import database_now
from .messages import js_slice
from .provenance import provenance_hash
from .quota import one, utc
from .runtime_policy import public_json
from .runtime_store import insert, update
from .sessions import iso

STATUSES = {
    "running",
    "candidate_complete",
    "verifying",
    "repair_required",
    "repairing",
    "completed",
    "failed",
    "cancelled",
}
TERMINAL = {"completed", "failed", "cancelled"}
NODE_STATUSES = {"pending", "running", "candidate_complete", "passed", "failed", "skipped"}
HASH = re.compile(r"sha256:[a-f0-9]{64}")


class MissionError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def handle(row):
    return {
        "id": row["id"],
        "generationId": str(row["generation_id"]),
        "projectId": row["project_id"],
        "requestId": row["request_id"],
        "status": row["status"],
        "version": row["version"],
        "candidateVersion": row["candidate_version"],
        "specHash": row["spec_hash"],
        "acceptedReceiptId": row["accepted_receipt_id"],
    }


def receipt_handle(row, generation):
    return {
        "id": row["id"],
        "missionId": row["mission_id"],
        "generationId": str(generation),
        "candidateVersion": row["candidate_version"],
        "receiptType": row["receipt_type"],
        "verdict": row["verdict"],
        "subjectHash": row["subject_hash"],
        "receiptHash": row["receipt_hash"],
        "createdAt": iso(row["created_at"]),
    }


async def bound(connection, ref):
    row = await one(
        connection,
        "SELECT * FROM agent_missions WHERE id=%s AND project_id=%s AND request_id=%s FOR UPDATE",
        (ref.get("missionId", ref.get("id")), ref["projectId"], ref["requestId"]),
    )
    if not row:
        raise MissionError(
            "MISSION_NOT_FOUND", "Mission does not exist or belongs to a different project/request."
        )
    if row["status"] not in STATUSES:
        raise MissionError("MISSION_STATUS_INVALID", "Mission has an unsupported durable status.")
    return row


async def node_status(connection, mission, key, status, now):
    if status not in NODE_STATUSES:
        raise MissionError("MISSION_STATUS_INVALID", "Unsupported Mission node status.")
    result = await connection.execute(
        """UPDATE agent_mission_nodes SET status=%s,version=version+1,
        started_at=CASE WHEN %s='running' THEN %s ELSE started_at END,
        finished_at=CASE WHEN %s='running' THEN NULL WHEN %s IN ('candidate_complete','passed','failed','skipped') THEN %s ELSE finished_at END,
        updated_at=clock_timestamp() WHERE mission_id=%s AND node_key=%s""",
        (status, status, now, status, status, now, mission, key),
    )
    if result.rowcount != 1:
        raise MissionError("MISSION_NODE_NOT_FOUND", "Mission node does not exist: " + key)


def claim_valid(owner, ttl):
    if (
        not isinstance(owner, str)
        or not owner
        or len(owner.encode()) > 256
        or "\n" in owner
        or "\r" in owner
        or type(ttl) is not int
        or not 0 < ttl <= 86400000
    ):
        raise MissionError(
            "MISSION_VERIFICATION_LEASE_INVALID", "Verification owner or TTL is invalid."
        )


def verify_fence(row, fence, now):
    if (
        row["status"] != "verifying"
        or row["verification_lease_owner"] != fence["leaseOwner"]
        or row["verification_fencing_token"] != fence["fencingToken"]
    ):
        raise MissionError(
            "MISSION_VERIFICATION_LEASE_LOST",
            "Verification owner no longer holds the Mission fence.",
        )
    if not row["verification_lease_expires_at"] or utc(row["verification_lease_expires_at"]) <= now:
        raise MissionError(
            "MISSION_VERIFICATION_LEASE_EXPIRED",
            "Verification lease expired before the durable write.",
        )


def replay_hash(payload):
    payload = deepcopy(payload)
    payload.pop("createdAt", None)
    if isinstance(payload.get("preview"), dict):
        payload["preview"].pop("readyAt", None)
    return provenance_hash(payload)


def parse_time(value):
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def clear_verification():
    return {
        "verification_lease_owner": None,
        "verification_lease_expires_at": None,
        "verification_last_heartbeat_at": None,
    }


class Missions:
    def __init__(self, database):
        self.database = database

    async def ensure(self, spec, *, mission_id=None, generation_id=None):
        digest = "sha256:" + provenance_hash(spec)
        async with self.database.transaction() as connection:
            project = await one(
                connection, "SELECT id FROM projects WHERE id=%s FOR UPDATE", (spec["projectId"],)
            )
            if not project:
                raise MissionError("MISSION_NOT_FOUND", "Mission project does not exist.")
            existing = await one(
                connection,
                "SELECT * FROM agent_missions WHERE project_id=%s AND request_id=%s",
                (spec["projectId"], spec["requestId"]),
            )
            if existing:
                if existing["spec_hash"] != digest:
                    raise MissionError(
                        "MISSION_SPEC_CONFLICT",
                        "Request already owns a Mission with a different immutable spec.",
                    )
                return handle(existing)
            if await one(
                connection,
                "SELECT id FROM agent_missions WHERE project_id=%s AND active_slot=1",
                (spec["projectId"],),
            ):
                raise MissionError(
                    "MISSION_PROJECT_BUSY", "The project already has a non-terminal Mission."
                )
            now = await database_now(connection)
            mission = await insert(
                connection,
                "agent_missions",
                {
                    "id": mission_id or "mission_" + str(uuid4()),
                    "generation_id": generation_id or str(uuid4()),
                    "project_id": spec["projectId"],
                    "request_id": spec["requestId"],
                    "status": "running",
                    "spec": Jsonb(spec),
                    "spec_hash": digest,
                    "updated_at": now,
                },
            )
            for node in spec["nodes"]:
                data = {
                    "id": str(uuid4()),
                    "mission_id": mission["id"],
                    "node_key": node["key"],
                    "node_type": node["type"],
                    "effect": node["effect"],
                    "status": "pending" if node["dependencies"] else "running",
                    "updated_at": now,
                    "started_at": parse_time(spec["createdAt"])
                    if not node["dependencies"]
                    else None,
                }
                for field, column in (
                    ("dependencies", "dependencies"),
                    ("allowedTools", "allowed_tools"),
                    ("requiredSkillSections", "required_skill_sections"),
                    ("inputArtifacts", "input_artifacts"),
                    ("outputArtifacts", "output_artifacts"),
                    ("budget", "budget"),
                    ("acceptancePredicates", "acceptance_predicates"),
                ):
                    data[column] = Jsonb(node[field])
                await insert(connection, "agent_mission_nodes", data)
            return handle(mission)

    async def read(self, project, request):
        row = await self.database.fetch_one(
            "SELECT * FROM agent_missions WHERE project_id=%s AND request_id=%s", (project, request)
        )
        return handle(row) if row else None

    async def spec(self, ref):
        async with self.database.transaction() as connection:
            return (await bound(connection, ref))["spec"]

    async def mark_node(self, ref, key, status):
        async with self.database.transaction() as connection:
            row = await bound(connection, ref)
            if row["status"] in TERMINAL:
                raise MissionError(
                    "MISSION_ALREADY_TERMINAL", "A terminal Mission node cannot be changed."
                )
            await node_status(connection, row["id"], key, status, await database_now(connection))
            return handle(row)

    async def candidate(self, ref, candidate):
        if not all(
            isinstance(candidate.get(key), str) and HASH.fullmatch(candidate[key])
            for key in ("workspaceSha256", "summarySha256")
        ):
            raise MissionError("CANDIDATE_HASH_INVALID", "Candidate hashes must be SHA-256 values.")
        for path in candidate["declaredArtifacts"] + candidate["verifiedArtifacts"]:
            if (
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or ".." in path.split("/")
            ):
                raise MissionError(
                    "CANDIDATE_ARTIFACT_INVALID",
                    "Candidate artifacts must remain workspace-relative.",
                )
        async with self.database.transaction() as connection:
            row = await bound(connection, ref)
            if row["status"] not in {"running", "repairing", "candidate_complete"}:
                raise MissionError(
                    "MISSION_CANDIDATE_TRANSITION_INVALID",
                    "Mission cannot accept a candidate in its current state.",
                )
            if candidate["sourceRequestId"] != row["request_id"] and not candidate[
                "sourceRequestId"
            ].startswith(row["request_id"] + "-validation-repair"):
                raise MissionError(
                    "MISSION_CANDIDATE_REQUEST_MISMATCH",
                    "Candidate source request is not part of this Mission.",
                )
            if row["status"] == "candidate_complete":
                existing = await one(
                    connection,
                    "SELECT * FROM agent_evidence_receipts WHERE mission_id=%s AND candidate_version=%s AND receipt_type='candidate' AND subject_hash=%s",
                    (row["id"], row["candidate_version"], candidate["workspaceSha256"]),
                )
                if existing:
                    return {
                        "mission": handle(row),
                        "receipt": receipt_handle(existing, row["generation_id"]),
                    }
            version = row["candidate_version"] + 1
            payload = {
                **candidate,
                "missionId": row["id"],
                "generationId": str(row["generation_id"]),
                "candidateVersion": version,
            }
            created = parse_time(candidate["submittedAt"])
            receipt = await insert(
                connection,
                "agent_evidence_receipts",
                {
                    "id": "receipt_" + str(uuid4()),
                    "mission_id": row["id"],
                    "candidate_version": version,
                    "receipt_type": "candidate",
                    "verdict": "candidate_complete",
                    "subject_hash": candidate["workspaceSha256"],
                    "receipt_hash": "sha256:" + provenance_hash(payload),
                    "source_run_id": candidate["sourceRunId"],
                    "source_request_id": candidate["sourceRequestId"],
                    "payload": Jsonb(payload),
                    "created_at": created,
                },
            )
            changed = await update(
                connection,
                "agent_missions",
                row["id"],
                {
                    "status": "candidate_complete",
                    "candidate_version": version,
                    "current_candidate_run_id": candidate["sourceRunId"],
                    "current_candidate_request_id": candidate["sourceRequestId"],
                    "candidate_submitted_at": created,
                    "verification_started_at": None,
                    **clear_verification(),
                    "error_code": None,
                    "error_message": None,
                },
                version=True,
            )
            await node_status(
                connection, row["id"], "workspace_generation", "candidate_complete", created
            )
            return {
                "mission": handle(changed),
                "receipt": receipt_handle(receipt, row["generation_id"]),
            }

    async def begin_verification(self, ref, owner, lease_ttl_ms=60000):
        claim_valid(owner, lease_ttl_ms)
        async with self.database.transaction() as connection:
            row = await bound(connection, ref)
            now = await database_now(connection)
            if (
                row["status"] == "verifying"
                and row["verification_lease_owner"]
                and row["verification_lease_expires_at"]
                and utc(row["verification_lease_expires_at"]) > now
            ):
                raise MissionError(
                    "MISSION_VERIFICATION_BUSY",
                    "Mission verification is already owned by another orchestrator.",
                )
            if row["status"] not in {"candidate_complete", "verifying"}:
                raise MissionError(
                    "MISSION_VERIFICATION_TRANSITION_INVALID",
                    "Mission is not ready for verification.",
                )
            expiry = now + timedelta(milliseconds=lease_ttl_ms)
            changed = await update(
                connection,
                "agent_missions",
                row["id"],
                {
                    "status": "verifying",
                    "verification_started_at": now,
                    "verification_lease_owner": owner,
                    "verification_lease_expires_at": expiry,
                    "verification_last_heartbeat_at": now,
                    "verification_fencing_token": row["verification_fencing_token"] + 1,
                    "error_code": None,
                    "error_message": None,
                },
                version=True,
            )
            await node_status(connection, row["id"], "validation", "running", now)
            return {
                "mission": handle(changed),
                "leaseOwner": owner,
                "fencingToken": changed["verification_fencing_token"],
                "leaseExpiresAt": iso(expiry),
            }

    async def heartbeat(self, ref, fence, lease_ttl_ms=60000):
        claim_valid(fence["leaseOwner"], lease_ttl_ms)
        async with self.database.transaction() as connection:
            row = await bound(connection, ref)
            now = await database_now(connection)
            verify_fence(row, fence, now)
            expiry = now + timedelta(milliseconds=lease_ttl_ms)
            await update(
                connection,
                "agent_missions",
                row["id"],
                {"verification_lease_expires_at": expiry, "verification_last_heartbeat_at": now},
            )
            return {"leaseExpiresAt": iso(expiry)}

    async def abandon(self, ref, fence):
        async with self.database.transaction() as connection:
            row = await bound(connection, ref)
            if row["status"] == "candidate_complete":
                return handle(row)
            # An expired owner may abandon, but a replaced owner cannot.
            if (
                row["status"] != "verifying"
                or row["verification_lease_owner"] != fence["leaseOwner"]
                or row["verification_fencing_token"] != fence["fencingToken"]
            ):
                raise MissionError(
                    "MISSION_VERIFICATION_LEASE_LOST",
                    "Only the current verification owner can abandon its claim.",
                )
            changed = await update(
                connection,
                "agent_missions",
                row["id"],
                {
                    "status": "candidate_complete",
                    "verification_started_at": None,
                    **clear_verification(),
                },
                version=True,
            )
            result = await connection.execute(
                "UPDATE agent_mission_nodes SET status='pending',version=version+1,started_at=NULL,finished_at=NULL,updated_at=now() WHERE mission_id=%s AND node_key='validation' AND status='running'",
                (row["id"],),
            )
            if result.rowcount != 1:
                raise MissionError(
                    "MISSION_NODE_NOT_FOUND", "Mission validation node could not be reset."
                )
            return handle(changed)

    async def repairing(self, ref):
        async with self.database.transaction() as connection:
            row = await bound(connection, ref)
            if row["status"] == "repairing":
                return handle(row)
            if row["status"] != "repair_required":
                raise MissionError(
                    "MISSION_REPAIR_TRANSITION_INVALID", "Mission does not require repair."
                )
            changed = await update(
                connection,
                "agent_missions",
                row["id"],
                {"status": "repairing", "error_code": None, "error_message": None},
                version=True,
            )
            await node_status(
                connection,
                row["id"],
                "workspace_generation",
                "running",
                await database_now(connection),
            )
            return handle(changed)

    async def evidence(self, ref, fence, decision):
        payload = public_json(decision["payload"])
        if decision["receiptHash"] != "sha256:" + provenance_hash(payload):
            raise MissionError(
                "EVIDENCE_RECEIPT_HASH_MISMATCH",
                "Evidence receipt hash does not match its bounded payload.",
            )
        async with self.database.transaction() as connection:
            row = await bound(connection, ref)
            if row["status"] == "completed" and row["accepted_receipt_id"]:
                receipt = await one(
                    connection,
                    "SELECT * FROM agent_evidence_receipts WHERE id=%s",
                    (row["accepted_receipt_id"],),
                )
                if receipt["receipt_hash"] != decision["receiptHash"]:
                    raise MissionError(
                        "EVIDENCE_CONFLICT",
                        "Completed Mission already owns a different acceptance receipt.",
                    )
                return {
                    "mission": handle(row),
                    "receipt": receipt_handle(receipt, row["generation_id"]),
                }
            if row["status"] != "verifying":
                raise MissionError(
                    "MISSION_EVIDENCE_TRANSITION_INVALID",
                    "Mission is not accepting verification evidence.",
                )
            verify_fence(row, fence, await database_now(connection))
            if decision["candidateVersion"] != row["candidate_version"]:
                raise MissionError(
                    "EVIDENCE_CANDIDATE_STALE", "Evidence belongs to an older candidate version."
                )
            expected = {
                "missionId": row["id"],
                "generationId": str(row["generation_id"]),
                "requestId": row["request_id"],
                "projectId": row["project_id"],
                "missionSpecSha256": row["spec_hash"],
            }
            if any(payload.get(key) != value for key, value in expected.items()):
                raise MissionError(
                    "EVIDENCE_MISSION_IDENTITY_MISMATCH",
                    "Evidence identity does not match the durable Mission.",
                )
            accepted = decision["verdict"] == "accepted"
            kind = "acceptance" if accepted else "validation"
            receipt = await one(
                connection,
                "SELECT * FROM agent_evidence_receipts WHERE mission_id=%s AND candidate_version=%s AND receipt_type=%s AND subject_hash=%s",
                (row["id"], row["candidate_version"], kind, decision["subjectHash"]),
            )
            if receipt and (
                receipt["verdict"] != decision["verdict"]
                or replay_hash(receipt["payload"]) != replay_hash(payload)
            ):
                raise MissionError(
                    "EVIDENCE_CONFLICT",
                    "Candidate already owns different evidence for this subject.",
                )
            created = parse_time(payload["createdAt"])
            if not receipt:
                receipt = await insert(
                    connection,
                    "agent_evidence_receipts",
                    {
                        "id": "receipt_" + str(uuid4()),
                        "mission_id": row["id"],
                        "candidate_version": row["candidate_version"],
                        "receipt_type": kind,
                        "verdict": decision["verdict"],
                        "subject_hash": decision["subjectHash"],
                        "receipt_hash": decision["receiptHash"],
                        "source_run_id": row["current_candidate_run_id"],
                        "source_request_id": row["current_candidate_request_id"],
                        "payload": Jsonb(payload),
                        "created_at": created,
                    },
                )
            if accepted:
                result = await connection.execute(
                    "UPDATE user_requests SET status='completed',completed_at=%s,error_message=NULL WHERE id=%s AND project_id=%s AND status IN ('pending','processing','active','running')",
                    (created, row["request_id"], row["project_id"]),
                )
                if result.rowcount != 1:
                    raise MissionError(
                        "MISSION_REQUEST_NOT_ACTIVE",
                        "Mission acceptance lost the race to a terminal user request state.",
                    )
            next_status = (
                "completed"
                if accepted
                else "repair_required"
                if decision["verdict"] == "repair_required"
                else "candidate_complete"
                if decision["verdict"] == "retry_infrastructure"
                else "failed"
            )
            verify_fence(row, fence, await database_now(connection))
            changes = {"status": next_status, **clear_verification()}
            if accepted:
                changes.update(
                    accepted_receipt_id=receipt["id"],
                    active_slot=None,
                    completed_at=created,
                    error_code=None,
                    error_message=None,
                )
            else:
                changes.update(
                    error_code=next(iter(decision["reasonCodes"]), "EVIDENCE_REJECTED"),
                    error_message=f"Evidence verdict: {decision['verdict']}.",
                )
                if next_status == "failed":
                    changes.update(active_slot=None, completed_at=created)
                    await connection.execute(
                        "UPDATE user_requests SET status='failed',completed_at=%s,error_message=%s WHERE id=%s AND project_id=%s AND status IN ('pending','processing','active','running')",
                        (created, changes["error_message"], row["request_id"], row["project_id"]),
                    )
            changed = await update(connection, "agent_missions", row["id"], changes, version=True)
            if accepted:
                for key in ("validation", "evidence_verification", "preview_readiness"):
                    await node_status(connection, row["id"], key, "passed", created)
            else:
                await node_status(
                    connection,
                    row["id"],
                    "validation",
                    "passed" if decision["verdict"] == "retry_infrastructure" else "failed",
                    created,
                )
                await node_status(connection, row["id"], "evidence_verification", "failed", created)
                if decision["verdict"] == "retry_infrastructure":
                    await node_status(connection, row["id"], "preview_readiness", "failed", created)
                elif next_status == "failed":
                    await connection.execute(
                        "UPDATE agent_mission_nodes SET status='failed',version=version+1,finished_at=%s,updated_at=%s WHERE mission_id=%s AND status IN ('pending','running')",
                        (created, created, row["id"]),
                    )
            return {
                "mission": handle(changed),
                "receipt": receipt_handle(receipt, row["generation_id"]),
            }

    async def terminal(
        self, ref, status, code, message, *, expected_version=None, expected_status=None
    ):
        if status not in {"failed", "cancelled"}:
            raise MissionError("MISSION_STATUS_INVALID", "Unsupported Mission terminal status.")
        async with self.database.transaction() as connection:
            row = await bound(connection, ref)
            if row["status"] in TERMINAL:
                return handle(row)
            if (expected_version is not None and row["version"] != expected_version) or (
                expected_status is not None and row["status"] != expected_status
            ):
                raise MissionError(
                    "MISSION_WRITE_CONFLICT", "Mission changed before terminal projection."
                )
            now = await database_now(connection)
            changed = await update(
                connection,
                "agent_missions",
                row["id"],
                {
                    "status": status,
                    "active_slot": None,
                    "error_code": code,
                    "error_message": js_slice(message, 0, 2000),
                    "completed_at": now,
                    **clear_verification(),
                },
                version=True,
            )
            await connection.execute(
                "UPDATE agent_mission_nodes SET status='failed',version=version+1,finished_at=%s,updated_at=%s WHERE mission_id=%s AND status IN ('pending','running')",
                (now, now, row["id"]),
            )
            return handle(changed)

    async def cancel_active(self, project, message="Mission was cancelled by the user."):
        async with self.database.transaction() as connection:
            now = await database_now(connection)
            rows = await (
                await connection.execute(
                    "SELECT * FROM agent_missions WHERE project_id=%s AND status NOT IN ('completed','failed','cancelled') FOR UPDATE",
                    (project,),
                )
            ).fetchall()
            for row in rows:
                await update(
                    connection,
                    "agent_missions",
                    row["id"],
                    {
                        "status": "cancelled",
                        "active_slot": None,
                        "error_code": "MISSION_CANCELLED",
                        "error_message": js_slice(message, 0, 2000),
                        "completed_at": now,
                        **clear_verification(),
                    },
                    version=True,
                )
                await connection.execute(
                    "UPDATE agent_mission_nodes SET status='failed',version=version+1,finished_at=%s,updated_at=%s WHERE mission_id=%s AND status IN ('pending','running')",
                    (now, now, row["id"]),
                )
            return len(rows)

    async def accepted_snapshot(self, project, request):
        row = await self.database.fetch_one(
            """SELECT m.*,r.receipt_hash,r.created_at AS receipt_created_at,r.payload AS receipt_payload FROM agent_missions m
            LEFT JOIN agent_evidence_receipts r ON r.id=m.accepted_receipt_id WHERE m.project_id=%s AND m.request_id=%s""",
            (project, request),
        )
        if not row:
            return None
        payload = row["receipt_payload"] if isinstance(row["receipt_payload"], dict) else {}
        preview = payload.get("preview") if isinstance(payload.get("preview"), dict) else {}
        return {
            "missionId": row["id"],
            "generationId": str(row["generation_id"]),
            "projectId": project,
            "requestId": request,
            "missionStatus": row["status"],
            "candidateVersion": row["candidate_version"],
            "acceptedReceiptId": row["accepted_receipt_id"],
            "acceptedReceiptHash": row["receipt_hash"],
            "acceptedAt": iso(row["receipt_created_at"]) if row["receipt_created_at"] else None,
            "previewUrl": preview.get("url") if isinstance(preview.get("url"), str) else None,
            "previewPort": preview.get("port")
            if type(preview.get("port")) in {int, float}
            else None,
        }
