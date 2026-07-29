"""Versioned, auditable switches for realtime memory interventions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


MEMORY_POLICY_VERSION = 1
MEMORY_SCOPES = {"case", "participant"}

_PROFILES: dict[str, dict[str, Any]] = {
    "all-online": {
        "preference": {"enabled": True, "scope": "participant"},
        "episodic": {"enabled": True, "scope": "case"},
    },
    "context-only": {
        "preference": {"enabled": False, "scope": "case"},
        "episodic": {"enabled": False, "scope": "case"},
    },
    "confirmed-preference": {
        "preference": {"enabled": True, "scope": "participant"},
        "episodic": {"enabled": False, "scope": "case"},
    },
    "lexical-episodic": {
        "preference": {"enabled": False, "scope": "case"},
        "episodic": {"enabled": True, "scope": "case"},
    },
}


def memory_profiles() -> list[dict[str, Any]]:
    labels = {
        "all-online": "全部在线记忆",
        "context-only": "仅会话上下文",
        "confirmed-preference": "上下文 + 确认偏好",
        "lexical-episodic": "上下文 + 词法情景记忆",
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

    for name in ("preference", "episodic"):
        override = raw_components.get(name)
        if override is None:
            continue
        if isinstance(override, bool):
            components[name]["enabled"] = override
            continue
        if not isinstance(override, Mapping):
            raise ValueError(f"memory component {name} must be a boolean or object")
        unknown = set(override) - {"enabled", "scope"}
        if unknown:
            raise ValueError(f"unknown {name} memory settings: {', '.join(sorted(unknown))}")
        if "enabled" in override:
            if not isinstance(override["enabled"], bool):
                raise ValueError(f"{name}.enabled must be a boolean")
            components[name]["enabled"] = override["enabled"]
        if "scope" in override:
            scope = str(override["scope"])
            if scope not in MEMORY_SCOPES:
                raise ValueError(f"{name}.scope must be case or participant")
            components[name]["scope"] = scope

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
