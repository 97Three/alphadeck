"""Reconcile expired attempts under the shared physical workspace lock, then replan."""

import re
from uuid import uuid4

from .mutation_journal import MutationConflict, list_journals
from .progress import normalize
from .provenance import provenance_hash
from .resource_lock import resource_lock
from .runtime_policy import RuntimeStoreError
from .runtime_store import RuntimeStore, fence_for


def assert_checkpoint(checkpoint):
    version = checkpoint["state_version"]
    if version < 2:
        return
    if version != 2:
        raise ValueError("Unsupported PI Agent checkpoint state version " + str(version))
    state = checkpoint["public_state"]
    if checkpoint["state_hash"] != "sha256:" + provenance_hash(state):
        raise ValueError("PI Agent checkpoint failed its state hash check.")
    if (
        checkpoint["boundary"] == "model_turn_completed"
        and state.get("stage") == "model_turn_completed"
    ):
        normalized = normalize(state["progressOracle"])
        fingerprints = [
            value
            for key in (
                "seenTrustedFactFingerprints",
                "seenWorkspaceFingerprints",
                "seenToolObservationFingerprints",
            )
            for value in normalized[key]
        ]
        if normalized["lastWorkspaceFingerprint"] is not None:
            fingerprints.append(normalized["lastWorkspaceFingerprint"])
        if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in fingerprints):
            raise ValueError("PI Agent checkpoint contains invalid progress fingerprints.")


async def reconcile_expired(
    database, project, root=None, *, owner=None, ttl_ms=60000, limit=100, already_locked=False
):
    if root is not None and not already_locked:
        async with resource_lock(
            root,
            {"purpose": "replan_recovery", "projectId": project},
            recover_dead_local_owner=True,
        ):
            return await reconcile_expired(
                database,
                project,
                root,
                owner=owner,
                ttl_ms=ttl_ms,
                limit=limit,
                already_locked=True,
            )
    if type(ttl_ms) is not int or ttl_ms <= 0:
        raise ValueError("PI Agent reconciliation leaseMs must be a positive safe integer.")
    store = RuntimeStore(database)
    owner = owner or "reconciler:" + str(uuid4())
    candidates = await store.reconciliation_candidates(project=project, limit=limit)
    journals = list_journals(root) if root is not None else []
    by_operation = {journal.manifest["operationId"]: journal for journal in journals}
    for journal in journals:
        execution = await database.fetch_one(
            "SELECT * FROM agent_tool_executions WHERE operation_id=%s",
            (journal.manifest["operationId"],),
        )
        if (
            not execution
            or execution["run_id"] != journal.manifest["runId"]
            or execution["effect"] != "workspace_write"
        ):
            raise ValueError(
                "PI Agent mutation journal does not match its durable workspace ledger entry."
            )
        expected = {"succeeded": "applied", "failed": "rolled_back"}.get(execution["status"])
        if expected:
            if journal.manifest["state"] != expected:
                raise ValueError(
                    "Terminal PI Agent workspace execution has an inconsistent journal."
                )
            journal.cleanup()
            del by_operation[journal.manifest["operationId"]]
    result = {"interruptedRunIds": [], "blocked": [], "racedRunIds": []}
    for candidate in candidates:
        if candidate["latestCheckpoint"]:
            assert_checkpoint(candidate["latestCheckpoint"])
        mutations = candidate["unresolvedToolExecutions"]
        blocked = []
        for execution in mutations:
            journal = by_operation.get(execution["operation_id"])
            recoverable = (
                root is not None
                and execution["effect"] == "workspace_write"
                and execution["status"] in {"prepared", "commit_authorized"}
                and (execution["status"] != "commit_authorized" or journal is not None)
                and (journal is None or journal.manifest["runId"] == execution["run_id"])
            )
            if not recoverable:
                blocked.append(execution["operation_id"])
        if blocked:
            result["blocked"].append({"runId": candidate["run"]["id"], "operationIds": blocked})
            continue
        try:
            row = await store.claim(
                candidate["run"]["id"],
                owner,
                candidate["run"]["version"],
                ttl_ms,
                allow_terminal_reconciliation=True,
            )
            for execution in mutations:
                journal = by_operation.get(execution["operation_id"])
                if journal:
                    journal.rollback()
                completed = await store.complete_tool(
                    fence_for(row),
                    execution["operation_id"],
                    "failed",
                    receipt={
                        "reconciliation": "workspace_rolled_back",
                        "journal": journal.manifest["schemaVersion"]
                        if journal
                        else "no_physical_commit",
                    },
                    error={
                        "code": "WORKSPACE_MUTATION_ROLLED_BACK",
                        "message": "Interrupted PI Agent workspace mutation was rolled back before replanning.",
                    },
                )
                row = completed["run"]
            await store.complete(
                fence_for(row),
                "interrupted",
                row["turn_count"],
                {
                    key: row[column]
                    for key, column in (
                        ("inputTokens", "input_tokens"),
                        ("outputTokens", "output_tokens"),
                        ("totalTokens", "total_tokens"),
                        ("cachedInputTokens", "cached_input_tokens"),
                        ("cacheMissInputTokens", "cache_miss_input_tokens"),
                        ("reasoningTokens", "reasoning_tokens"),
                    )
                },
                error={
                    "code": "LEASE_EXPIRED_REPLAN_REQUIRED",
                    "message": "Expired PI Agent attempt was closed; recovery requires a new replan run.",
                },
            )
            for execution in mutations:
                if journal := by_operation.pop(execution["operation_id"], None):
                    journal.cleanup()
            result["interruptedRunIds"].append(row["id"])
        except MutationConflict:
            result["blocked"].append(
                {
                    "runId": candidate["run"]["id"],
                    "operationIds": [value["operation_id"] for value in mutations],
                }
            )
        except RuntimeStoreError as error:
            if error.code not in {"CONFLICT", "LEASE_LOST", "INVALID_STATE", "WORKSPACE_BUSY"}:
                raise
            result["racedRunIds"].append(candidate["run"]["id"])
    return result
