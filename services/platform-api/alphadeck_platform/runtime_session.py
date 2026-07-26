import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime

from .mutation_journal import list_journals
from .provenance import provenance_hash
from .quota import Quota
from .resource_lock import resource_lock
from .runtime_store import RuntimeStore, fence_for


class RuntimeSession:
    def __init__(self, database, row, *, lease_ttl_ms=60000, heartbeat_seconds=15):
        if heartbeat_seconds * 1000 >= lease_ttl_ms:
            raise ValueError("Heartbeat interval must be smaller than lease TTL")
        self.database, self.store, self.row = database, RuntimeStore(database), row
        self.lock = asyncio.Lock()
        self.lease_ttl_ms, self.interval = lease_ttl_ms, heartbeat_seconds
        self.fatal = None
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self._heartbeats())
        return self

    async def __aexit__(self, *_error):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def healthy(self):
        if self.fatal:
            raise self.fatal

    async def _heartbeats(self):
        try:
            while True:
                await asyncio.sleep(self.interval)
                async with self.lock:
                    self.row = await self.store.heartbeat(fence_for(self.row), self.lease_ttl_ms)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fatal = error

    async def event(self, kind, payload, *, usage=None, transition=None):
        async with self.lock:
            self.healthy()
            sequence = self.row["last_event_sequence"] + 1
            event_id = str(self.row["run_instance_id"]) + ":" + str(sequence)
            result = await self.store.append(
                fence_for(self.row),
                event_id,
                sequence,
                kind,
                payload,
                datetime.now(UTC),
                cumulative_usage=usage,
                transition=transition,
            )
            self.row = result["run"]
            return result["event"]

    async def checkpoint(self, turn, boundary, state):
        async with self.lock:
            self.healthy()
            result = await self.store.checkpoint(
                fence_for(self.row),
                self.row["last_event_sequence"],
                turn,
                boundary,
                state,
                "sha256:" + provenance_hash(state),
                state_version=2,
            )
            self.row = result["run"]

    async def prepare(self, operation, call, definition, input_hash):
        async with self.lock:
            self.healthy()
            result = await self.store.prepare_tool(
                fence_for(self.row),
                operation,
                call["id"],
                call["name"],
                input_hash,
                definition["effect"],
                definition["idempotency"],
                idempotency_key=operation if definition["idempotency"] == "operation_key" else None,
            )
            self.row = result["run"]
            if not result["created"]:
                raise RuntimeError(
                    "Tool execution already exists; recovery must replan instead of replaying"
                )
            return result["execution"]

    async def authorize(self, operation, commit):
        async with self.lock:
            self.healthy()
            return await self.store.authorize_mutation(fence_for(self.row), operation, commit)

    async def tool_finished(self, operation, result, *, uncertain=False, workspace=None):
        receipt = {"ok": result["ok"], "contentSha256": provenance_hash(result.get("content", ""))}
        error = None if result["ok"] else {key: result["error"][key] for key in ("code", "message")}
        async with self.lock:
            self.healthy()
            completed = await self.store.complete_tool(
                fence_for(self.row),
                operation,
                "succeeded" if result["ok"] else "uncertain" if uncertain else "failed",
                receipt=receipt,
                error=error,
            )
            self.row = completed["run"]
        if workspace and not uncertain:
            async with resource_lock(
                workspace,
                {"purpose": "workspace_write", "runId": self.row["id"], "operationId": operation},
                recover_dead_local_owner=True,
            ):
                for journal in list_journals(workspace):
                    if journal.manifest["operationId"] == operation and journal.manifest[
                        "state"
                    ] in {"applied", "rolled_back"}:
                        journal.cleanup()

    async def complete(self, status, turns, usage, error=None):
        async with self.lock:
            self.healthy()
            self.row = await self.store.complete(
                fence_for(self.row), status, turns, usage, error=error
            )
        if self.row["actor_user_id"] and self.row["total_tokens"] > 0:
            try:
                await Quota(self.database).record_usage(
                    self.row["actor_user_id"],
                    "llm.total_tokens.monthly",
                    self.row["total_tokens"],
                    "agent-run:" + self.row["id"] + ":total-tokens",
                    "agent_run",
                    source_id=self.row["id"],
                    project_id=self.row["project_id"],
                    at=self.row["finished_at"],
                    metadata={"provider": self.row["provider"], "model": self.row["model"]},
                )
            except Exception:
                logging.exception("Agent token usage requires later reconciliation")
        return self.row
