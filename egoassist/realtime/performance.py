"""Measured audit projections. No estimated end-to-end latency or confidence."""

from __future__ import annotations

import math
from statistics import median
from typing import Any


def event_metric(event: dict[str, Any]) -> dict[str, Any] | None:
    kind = event.get("type")
    payload = event.get("payload") or {}
    name, value = None, None
    if kind == "control_decision":
        name, value = "routerMs", payload.get("decision", {}).get("latencyMs")
    elif kind == "episodic.memory.retrieved":
        name, value = "retrievalMs", payload.get("retrievalMs")
    elif kind in {"timeline.memory.written", "timeline.memory.capture.started", "timeline.memory.write.skipped"}:
        name, value = "writePolicyMs", payload.get("writePolicyMs")
    elif kind in {"response.first_audio", "response.first_text", "response.first_transcript"}:
        name = "responseFirstAudioMs" if kind.endswith("audio") else "responseFirstTextMs"
        value = event.get("latencyMs")
    elif kind == "insight.result":
        name, value = "insightMs", payload.get("latencyMs")
    if not name or type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return None
    return {"name": name, "value": round(value, 3), "eventId": event.get("eventId"),
            "responseId": event.get("responseId"), "timestamp": event.get("timestampWall")}


def performance_snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = {}
    samples: dict[str, list[float]] = {}
    timeouts = 0
    usage = None
    for event in events:
        measurement = event_metric(event)
        if measurement:
            name = measurement["name"]
            samples.setdefault(name, []).append(measurement["value"])
            metrics[name] = measurement
        payload = event.get("payload") or {}
        if event.get("type") == "response.watchdog.timeout" or (event.get("type") == "insight.result" and payload.get("timedOut")) or (
            event.get("type") == "control_decision" and payload.get("decision", {}).get("timedOut")
        ):
            timeouts += 1
        if event.get("type") == "response.finished" and payload.get("usage"):
            usage = {"responseId": event.get("responseId"), "eventId": event.get("eventId"),
                     "status": payload.get("status"), **payload["usage"]}
    for name, values in samples.items():
        metrics[name].update(samples=len(values), median=round(median(values), 3),
                             minimum=min(values), maximum=max(values))
    return {"metrics": metrics, "latestUsage": usage, "timeouts": timeouts,
            "windowEvents": len(events), "windowLimit": 160,
            "claimBoundary": "Measured stages from the current session's latest 160 events. Response first output starts at response request, excluding routing, microphone endpointing and playback. Separate stages may belong to different turns; do not sum them."}
