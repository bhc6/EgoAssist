"""Machine-readable product capability truth.

Manifests may describe desired capabilities, but availability is derived from
implemented product layers. This module keeps unsupported and demo adapters from
being exposed as executable product features.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


CAPABILITY_STATUSES = {
    "available",
    "experimental",
    "descriptive",
    "demo-only",
    "unsupported",
    "unconfigured",
    "offline-only",
}

EXECUTABLE_STATUS = "available"


PLATFORM_CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "id": "realtime-media",
        "label": "Full-duplex media and interruption",
        "status": "available",
        "reason": "Browser capture, provider lanes, response IDs, and barge-in are connected.",
    },
    {
        "id": "durable-transcripts",
        "label": "Durable completed transcripts",
        "status": "available",
        "reason": "Provider-completed transcripts are persisted before semantic processing.",
    },
    {
        "id": "scoped-memory",
        "label": "Scoped behavioral memory",
        "status": "available",
        "reason": "TimelineMemory and MemoryDraft enforce participant and case ownership.",
    },
    {
        "id": "background-execution",
        "label": "Durable background execution",
        "status": "unsupported",
        "reason": "No production workflow executor is connected yet.",
    },
    {
        "id": "timer-delivery",
        "label": "Timer and reminder delivery",
        "status": "unsupported",
        "reason": "Deadline calculation is not a durable timer or notification channel.",
    },
    {
        "id": "human-help",
        "label": "Remote human help",
        "status": "demo-only",
        "reason": "The bundled adapter has no external helper or acknowledgement endpoint.",
    },
    {
        "id": "reference-display",
        "label": "Task reference display",
        "status": "unsupported",
        "reason": "No artifact resolver or viewer is connected yet.",
    },
    {
        "id": "structured-perception",
        "label": "Checkpoint-constrained visual perception",
        "status": "available",
        "reason": "Fresh frames are bound to task checkpoints, allowed paths, artifact provenance, and user-confirmed advancement.",
    },
    {
        "id": "online-evaluation",
        "label": "Outcome-based online evaluation",
        "status": "offline-only",
        "reason": "Current metrics consume caller-provided replay labels rather than live truth.",
    },
)


def _status(value: Any, *, default: str) -> str:
    normalized = str(value or default).strip().casefold()
    if normalized not in CAPABILITY_STATUSES:
        raise ValueError(f"invalid capability status: {normalized}")
    return normalized


def tool_truth(declaration: Mapping[str, Any]) -> dict[str, Any]:
    raw = declaration.get("availability")
    availability = dict(raw) if isinstance(raw, Mapping) else {}
    status = _status(availability.get("status"), default="available")
    reason = str(
        availability.get("reason")
        or (
            "A configured executor is connected."
            if status == EXECUTABLE_STATUS
            else "This tool is not connected to a production executor."
        )
    )
    return {
        "status": status,
        "executable": status == EXECUTABLE_STATUS,
        "reason": reason,
    }


def public_tool_declaration(declaration: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(declaration), "availability": tool_truth(declaration)}


def executable_tool_declarations(
    declarations: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in declarations
        if tool_truth(item)["executable"]
    ]


def skill_capability_truth(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    configured = manifest.get("capability_status")
    status_by_name = dict(configured) if isinstance(configured, Mapping) else {}
    values = []
    for raw_name in manifest.get("capabilities", []):
        name = str(raw_name)
        raw = status_by_name.get(name)
        detail = dict(raw) if isinstance(raw, Mapping) else {}
        status = _status(detail.get("status"), default="descriptive")
        values.append({
            "id": name,
            "status": status,
            "executable": status == EXECUTABLE_STATUS,
            "reason": str(
                detail.get("reason")
                or "Manifest metadata describes desired behavior; no capability dispatcher is connected."
            ),
        })
    return values


def platform_capability_truth(
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    configured = dict(overrides or {})
    return [
        {
            **item,
            **dict(configured.get(str(item["id"]), {})),
            "executable": str(
                configured.get(str(item["id"]), {}).get("status", item["status"])
            ) == EXECUTABLE_STATUS,
        }
        for item in PLATFORM_CAPABILITIES
    ]


def platform_capability_available(
    capability_id: str,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
    return any(
        item["id"] == str(capability_id) and item["status"] == EXECUTABLE_STATUS
        for item in platform_capability_truth(overrides)
    )
