"""Account capabilities intersect with project membership, as in the TS evaluator."""

import json
from datetime import UTC, datetime

from .config import ROOT

CATALOG = json.loads((ROOT / "config/access-control.json").read_text(encoding="utf-8"))


def active(grant: dict, now: datetime) -> bool:
    if grant.get("permissionKey") not in CATALOG["actions"] or grant.get("effect") not in {
        "allow",
        "deny",
    }:
        return False
    expiration = grant.get("expiresAt")
    if not expiration:
        return True
    try:
        timestamp = (
            expiration
            if isinstance(expiration, datetime)
            else datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        )
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp > now
    except (ValueError, TypeError, AttributeError):
        return grant["effect"] == "deny"


def matching(grants: list[dict], action: str, now: datetime) -> list[str]:
    return [
        grant["effect"]
        for grant in grants
        if grant.get("permissionKey") == action and active(grant, now)
    ]


def evaluate_permission(data: dict, now: datetime | None = None) -> dict:
    action = data.get("action")
    if not isinstance(action, str) or action not in CATALOG["actions"]:
        return {
            "allowed": False,
            "action": str(action or ""),
            "reason": "UNKNOWN_ACTION",
            "matchedSources": [],
        }
    now = now or datetime.now(UTC)
    policy = data.get("policy") or {}
    profile = policy.get("profile") or {}
    actor = data["actor"]
    project = data.get("project") or {}
    profile_key = profile.get(
        "key", actor.get("permissionProfileKey", CATALOG["defaultMemberProfile"])
    )
    scope = CATALOG["actions"][action]["scope"]
    base = {"action": action, "scope": scope, "profileKey": profile_key}

    def decision(allowed: bool, reason: str, sources: list[str]) -> dict:
        return {**base, "allowed": allowed, "reason": reason, "matchedSources": sources}

    if scope == "project" and not project.get("id"):
        return decision(False, "PROJECT_CONTEXT_REQUIRED", [])
    if actor.get("platformRole") == "admin":
        return decision(True, "ADMIN_ALL", ["platform-role:admin"])
    if profile:
        profile_effects = matching(profile.get("grants", []), action, now)
    else:
        rules = CATALOG["profiles"].get(profile_key, {})
        profile_effects = [
            effect for effect in ("allow", "deny") if action in rules.get(effect, [])
        ]
    user_effects = matching(policy.get("userOverrides") or [], action, now)
    membership_effects = matching(policy.get("membershipOverrides") or [], action, now)
    denied = []
    if "deny" in profile_effects:
        denied.append(f"profile:{profile_key}:deny")
    if "deny" in user_effects:
        denied.append("user-override:deny")
    if "deny" in membership_effects:
        denied.append("membership-override:deny")
    if denied:
        return decision(False, "EXPLICIT_DENY", denied)
    global_sources = []
    if "allow" in profile_effects:
        global_sources.append(f"profile:{profile_key}:allow")
    if "allow" in user_effects:
        global_sources.append("user-override:allow")
    if not global_sources:
        return decision(False, "PROFILE_NOT_GRANTED", [])
    if scope != "project":
        return decision(True, "GRANTED", global_sources)
    role = project.get("role")
    rules = CATALOG["projectRoles"].get(role)
    if rules is None:
        return decision(False, "PROJECT_MEMBERSHIP_REQUIRED", global_sources)
    if action in rules["deny"]:
        return decision(False, "EXPLICIT_DENY", [f"project-role:{role}:deny"])
    project_sources = []
    if action in rules["allow"]:
        project_sources.append(f"project-role:{role}:allow")
    if "allow" in membership_effects:
        project_sources.append("membership-override:allow")
    if not project_sources:
        return decision(False, "PROJECT_ROLE_NOT_GRANTED", global_sources)
    return decision(True, "GRANTED", global_sources + project_sources)
