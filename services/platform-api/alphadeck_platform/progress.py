"""Replayable verifiable-progress reducer from the original PI Agent runtime."""

from copy import deepcopy


def integer(value, label, minimum=0):
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise ValueError(f"{label} must be a safe integer >= {minimum}")
    return value


def fingerprint(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must contain a non-empty fingerprint")
    return value


def unique(values, label):
    if not isinstance(values, list):
        raise ValueError(f"{label} must be an array of fingerprints")
    return sorted({fingerprint(value, label) for value in values})


def initial_state():
    return {
        "version": 1,
        "turnsObserved": 0,
        "consecutiveNoProgressTurns": 0,
        "seenTrustedFactFingerprints": [],
        "seenWorkspaceFingerprints": [],
        "lastWorkspaceFingerprint": None,
        "lastFailedCheckCount": None,
        "seenToolObservationFingerprints": [],
    }


def normalize(state):
    state = deepcopy(state)
    if state["version"] != 1:
        raise ValueError("Unsupported ProgressOracle state version")
    integer(state["turnsObserved"], "turnsObserved")
    integer(state["consecutiveNoProgressTurns"], "consecutiveNoProgressTurns")
    if state["consecutiveNoProgressTurns"] > state["turnsObserved"]:
        raise ValueError("consecutiveNoProgressTurns cannot exceed turnsObserved")
    for key in (
        "seenTrustedFactFingerprints",
        "seenWorkspaceFingerprints",
        "seenToolObservationFingerprints",
    ):
        state[key] = unique(state[key], key)
    workspace = state["lastWorkspaceFingerprint"]
    if workspace is not None:
        fingerprint(workspace, "lastWorkspaceFingerprint")
        state["seenWorkspaceFingerprints"] = sorted(
            set(state["seenWorkspaceFingerprints"]) | {workspace}
        )
    if state["lastFailedCheckCount"] is not None:
        integer(state["lastFailedCheckCount"], "lastFailedCheckCount")
    return state


def evaluate_turn(previous, observation, stall_after=2):
    state = normalize(previous)
    integer(stall_after, "stallAfterConsecutiveNoProgressTurns", 1)
    facts = unique(observation.get("trustedFactFingerprints", []), "trustedFactFingerprints")
    new_facts = sorted(set(facts) - set(state["seenTrustedFactFingerprints"]))
    workspace_observed = "workspaceFingerprint" in observation
    workspace = (
        fingerprint(observation["workspaceFingerprint"], "workspaceFingerprint")
        if workspace_observed
        else None
    )
    last_workspace = state["lastWorkspaceFingerprint"]
    workspace_changed = (
        workspace is not None and last_workspace is not None and workspace != last_workspace
    )
    workspace_revisited = workspace_changed and workspace in state["seenWorkspaceFingerprints"]
    workspace_advanced = workspace_changed and not workspace_revisited
    failed = (
        integer(observation["failedCheckCount"], "failedCheckCount")
        if "failedCheckCount" in observation
        else None
    )
    previous_failed = state["lastFailedCheckCount"]
    change = None if failed is None or previous_failed is None else failed - previous_failed
    increased = change is not None and change > 0
    tools = observation.get("toolObservationFingerprints", [])
    if not isinstance(tools, list):
        raise ValueError("toolObservationFingerprints must be an array")
    prior_tools = set(state["seenToolObservationFingerprints"])
    seen_this_turn, novel, repeated = set(), set(), set()
    for tool in tools:
        fingerprint(tool, "toolObservationFingerprints")
        if tool in prior_tools or tool in seen_this_turn:
            repeated.add(tool)
        else:
            novel.add(tool)
        seen_this_turn.add(tool)
    writes = integer(observation.get("successfulWorkspaceWrites", 0), "successfulWorkspaceWrites")
    progress_signals = []
    if new_facts:
        progress_signals.append("trusted_fact_added")
    if workspace_advanced and not increased:
        progress_signals.append("workspace_advanced")
    if change is not None and change < 0:
        progress_signals.append("failed_checks_reduced")
    progressed = bool(progress_signals)
    no_progress = 0 if progressed else state["consecutiveNoProgressTurns"] + 1
    stall_signals = []
    if not progressed:
        stall_signals.append("no_verifiable_progress")
    if repeated:
        stall_signals.append("repeated_tool_observation")
    if workspace_revisited:
        stall_signals.append("workspace_revisited")
    if increased:
        stall_signals.append("failed_checks_increased")
    if writes and not progressed:
        stall_signals.append("successful_write_without_verifiable_progress")
    next_state = {
        "version": 1,
        "turnsObserved": state["turnsObserved"] + 1,
        "consecutiveNoProgressTurns": no_progress,
        "seenTrustedFactFingerprints": sorted(
            set(state["seenTrustedFactFingerprints"]) | set(facts)
        ),
        "seenWorkspaceFingerprints": sorted(
            set(state["seenWorkspaceFingerprints"])
            | ({workspace} if workspace is not None else set())
        ),
        "lastWorkspaceFingerprint": workspace if workspace is not None else last_workspace,
        "lastFailedCheckCount": failed if failed is not None else previous_failed,
        "seenToolObservationFingerprints": sorted(prior_tools | novel | repeated),
    }
    return {
        "state": next_state,
        "decision": {
            "turn": next_state["turnsObserved"],
            "progressed": progressed,
            "stalled": no_progress >= stall_after,
            "consecutiveNoProgressTurns": no_progress,
            "stallAfterConsecutiveNoProgressTurns": stall_after,
            "progressSignals": progress_signals,
            "stallSignals": sorted(stall_signals),
            "newTrustedFactFingerprints": new_facts,
            "workspaceObserved": workspace_observed,
            "workspaceChanged": workspace_changed,
            "workspaceAdvanced": workspace_advanced,
            "workspaceRevisited": workspace_revisited,
            "previousFailedCheckCount": previous_failed,
            "failedCheckCount": failed,
            "failedCheckCountChange": change,
            "novelToolObservationFingerprints": sorted(novel),
            "repeatedToolObservationFingerprints": sorted(repeated),
            "successfulWorkspaceWrites": writes,
        },
    }
