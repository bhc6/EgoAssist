"""Provider-neutral sequential evaluation for proactive embodied assistants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class InterventionRecord:
    expected: str
    predicted: str
    memory_expected: bool = False
    memory_used: bool = False
    latency_ms: float | None = None
    interrupted: bool = False


def summarize_interventions(records: Iterable[InterventionRecord]) -> dict[str, float]:
    items = list(records)
    if not items:
        return {
            "intervention_precision": 0.0,
            "unnecessary_interruption_rate": 0.0,
            "followup_reuse": 0.0,
            "barge_in_success": 0.0,
            "mean_latency_ms": 0.0,
        }
    interventions = [item for item in items if item.predicted != "silent"]
    useful = [item for item in interventions if item.expected != "silent"]
    silent_expected = [item for item in items if item.expected == "silent"]
    unnecessary = [item for item in silent_expected if item.predicted != "silent"]
    reuse_expected = [item for item in items if item.memory_expected]
    reuse_hits = [item for item in reuse_expected if item.memory_used]
    interruption_trials = [item for item in items if item.predicted != "silent"]
    latencies = [item.latency_ms for item in items if item.latency_ms is not None]
    return {
        "intervention_precision": len(useful) / len(interventions) if interventions else 1.0,
        "unnecessary_interruption_rate": len(unnecessary) / len(silent_expected) if silent_expected else 0.0,
        "followup_reuse": len(reuse_hits) / len(reuse_expected) if reuse_expected else 0.0,
        "barge_in_success": sum(item.interrupted for item in interruption_trials) / len(interruption_trials) if interruption_trials else 0.0,
        "mean_latency_ms": sum(latencies) / len(latencies) if latencies else 0.0,
    }
