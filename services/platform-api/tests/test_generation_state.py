import asyncio
import json
from pathlib import Path

import pytest

from alphadeck_platform.generation_state import GenerationState
from alphadeck_platform.generation_terminal import reconcile_dispatch, terminal_snapshot

FIXTURE = json.loads((Path(__file__).parent / "fixtures/generation.json").read_text())


def test_terminal_acceptance_and_preview_truth_table_matches_original():
    for case in FIXTURE["terminal"]:
        assert terminal_snapshot(**case["input"]) == case["expected"], case["input"]
    for case in FIXTURE["dispatch"]:
        assert reconcile_dispatch(case["generation"], case["job"]) == case["expected"]


def test_generation_state_and_cancelled_projection_match_original(tmp_path):
    async def run():
        clock = None
        state = GenerationState(tmp_path, clock=lambda: clock)
        for case in FIXTURE["timeline"]:
            clock = case["clock"]
            action = case["action"]
            if action["kind"] == "start":
                result = await state.start(
                    "project",
                    "request",
                    action["instruction"],
                    cli=action.get("cliPreference"),
                    model=action.get("selectedModel"),
                )
            elif action["kind"] == "repair":
                result = await state.increment_repair("project", "request")
            elif action["kind"] == "cancel":
                result = await state.update(
                    "project",
                    "request",
                    "agent_execution",
                    "failed",
                    action["reason"],
                    run_status="cancelled",
                    error_message=action["reason"],
                )
            else:
                result = await state.update(
                    "project",
                    "request",
                    action["stepId"],
                    action["status"],
                    action["summary"],
                    metadata=action.get("metadata"),
                    run_status=action.get("runStatus"),
                )
            assert result == case["output"]
            assert state.read() == case["state"]
        assert (tmp_path / ".data-agent/events.jsonl").is_file()

    asyncio.run(run())


def test_generation_state_authority_must_approve_before_physical_projection(tmp_path):
    async def run():
        calls = []

        async def reject(operation):
            calls.append(operation)
            raise RuntimeError("Generation lease lost")

        state = GenerationState(tmp_path, authority=reject)
        with pytest.raises(RuntimeError, match="lease lost"):
            await state.start("project", "request", "test")
        assert len(calls) == 1 and state.read() is None
        assert not (tmp_path / ".data-agent").exists()

    asyncio.run(run())
