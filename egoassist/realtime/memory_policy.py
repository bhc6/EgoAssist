"""Versioned, auditable switches for realtime memory interventions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from ..memory.timeline import MEMORY_SCOPES


MEMORY_POLICY_VERSION = 2

# Single unified 'timeline' component replaces the former split
# 'preference' + 'episodic' components.  Both sub-flags are now fields
# on the same object, matching the unified RealtimeEpisodeMemory API.
_PROFILES: dict[str, dict[str, Any]] = {
    "all-online": {
        "timeline": {
            "enabled": True,
            "preference_enabled": True,
            "episodic_enabled": True,
            "scope": "case",
        },
    },
    "context-only": {
        "timeline": {
            "enabled": False,
            "preference_enabled": False,
            "episodic_enabled": False,
            "scope": "case",
        },
    },
    "confirmed-preference": {
        "timeline": {
            "enabled": True,
            "preference_enabled": True,
            "episodic_enabled": False,
            "scope": "participant",
        },
    },
    "lexical-episodic": {
        "timeline": {
            "enabled": True,
            "preference_enabled": False,
            "episodic_enabled": True,
            "scope": "case",
        },
    },
}


def memory_profiles() -> list[dict[str, Any]]:
    labels = {
        "all-online": "全部在线记忆",
        "context-only": "仅会话上下文",
        "confirmed-preference": "上下文 + 参与者时间线",
        "lexical-episodic": "上下文 + 案例时间线",
    }
    return [
        {
            "id": profile_id,
            "label": labels[profile_id],
            "configuration": normalize_memory_policy({"profile": profile_id}),
        }
        for profile_id in _PROFILES
    ]


def default_memory_policy() -> dict[str, Any]:
    return normalize_memory_policy({"profile": "all-online"})


def normalize_memory_policy(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(value or {})
    requested_profile = str(raw.get("profile", "all-online")).strip()
    if requested_profile not in {*_PROFILES, "custom"}:
        raise ValueError(f"unknown memory profile: {requested_profile}")

    base_profile = requested_profile if requested_profile in _PROFILES else "all-online"
    components = deepcopy(_PROFILES[base_profile])
    raw_components = raw.get("components", {})
    if raw_components is None:
        raw_components = {}
    if not isinstance(raw_components, Mapping):
        raise ValueError("memory components must be an object")

    override = raw_components.get("timeline")
    if override is not None:
        if isinstance(override, bool):
            components["timeline"]["enabled"] = override
        elif not isinstance(override, Mapping):
            raise ValueError("memory component timeline must be a boolean or object")
        else:
            allowed = {"enabled", "preference_enabled", "episodic_enabled", "scope"}
            unknown = set(override) - allowed
            if unknown:
                raise ValueError(
                    f"unknown timeline memory settings: {', '.join(sorted(unknown))}"
                )
            for flag in ("enabled", "preference_enabled", "episodic_enabled"):
                if flag in override:
                    if not isinstance(override[flag], bool):
                        raise ValueError(f"timeline.{flag} must be a boolean")
                    components["timeline"][flag] = override[flag]
            if "scope" in override:
                scope = str(override["scope"])
                if scope not in MEMORY_SCOPES:
                    raise ValueError("timeline.scope must be case or participant")
                components["timeline"]["scope"] = scope

    # Auto-compute top-level 'enabled': True if either sub-flag is True.
    tl = components["timeline"]
    if not tl["preference_enabled"] and not tl["episodic_enabled"]:
        tl["enabled"] = False

    matched_profile = next(
        (profile_id for profile_id, candidate in _PROFILES.items() if candidate == components),
        "custom",
    )
    return {
        "version": MEMORY_POLICY_VERSION,
        "profile": matched_profile,
        "components": components,
        "infrastructure": {
            "contextJournal": "required",
            "contextCompaction": "deterministic-extractive-v1",
        },
        "claimBoundary": (
            "Switches gate behavioral memory persistence, retrieval, and injection. "
            "The append-only context journal remains enabled for recovery and audit."
        ),
    }
