"""Source-bound messages exchanged by observation and interaction."""
from dataclasses import dataclass
from .contracts import AmbientFrameContext, AmbientObservationHypothesis, PerceptionFrameContext, PerceptionHypothesis, ObservationEvidence

@dataclass(frozen=True)
class AcceptedObservation:
    captured: AmbientFrameContext | PerceptionFrameContext
    hypothesis: AmbientObservationHypothesis | PerceptionHypothesis
    evidence: ObservationEvidence
    entry: dict
    reason: str
