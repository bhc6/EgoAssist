"""Versioned product contracts shared by transports, workflows, and projections."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProductContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CorrelationIds(ProductContract):
    turn_id: str | None = Field(default=None, min_length=1)
    intent_id: str | None = Field(default=None, min_length=1)
    workflow_id: str | None = Field(default=None, min_length=1)
    job_id: str | None = Field(default=None, min_length=1)
    result_id: str | None = Field(default=None, min_length=1)
    trace_id: str | None = Field(default=None, min_length=1)


class ConversationTurn(ProductContract):
    turn_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    source: str = Field(min_length=1)
    provider_item_id: str | None = None
    language: str | None = None
    retention_class: str = "transcript"
    status: Literal["received", "classifying", "ready", "processing", "completed", "failed", "cancelled"] = "received"
    recorded_at: datetime
    correlation: CorrelationIds = Field(default_factory=CorrelationIds)
    provenance: dict[str, Any] = Field(default_factory=dict)


class IntentCandidate(ProductContract):
    kind: Literal[
        "foreground-dialogue",
        "background-job",
        "background-reminder",
        "persistent-task",
        "waiting-input",
    ]
    text: str = Field(min_length=1, max_length=500)
    parameters: dict[str, Any] = Field(default_factory=dict)


class JobRecord(ProductContract):
    job_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    workflow_id: str | None = None
    executor_kind: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: Literal["queued", "running", "suspended", "waiting-input", "waiting-result", "completed", "failed", "cancelled"] = "queued"
    deadline: datetime | None = None
    cancellation_policy: str = "explicit-only"
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation: CorrelationIds = Field(default_factory=CorrelationIds)


class ResultRecord(ProductContract):
    result_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    job_id: str | None = None
    status: Literal["pending", "displayed", "speaking", "delivered", "dismissed"] = "pending"
    content: Any
    priority: int = 50
    delivery: str = "speech-or-display"
    correlation: CorrelationIds = Field(default_factory=CorrelationIds)
    provenance: dict[str, Any] = Field(default_factory=dict)


class NotificationRequest(ProductContract):
    notification_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    result_id: str | None = None
    channel: Literal["browser", "ntfy", "webhook", "in-app"]
    deduplication_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class NotificationReceipt(ProductContract):
    notification_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    status: Literal["submitted", "acknowledged", "failed", "timed-out"]
    provider_message_id: str | None = None
    acknowledged_at: datetime | None = None
    error: str | None = None


class ArtifactDescriptor(ProductContract):
    artifact_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    kind: Literal["reference", "image", "document", "structured-output"]
    uri: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    checksum: str | None = None
    step_id: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class HumanHelpRequest(ProductContract):
    request_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    connector: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    preview: dict[str, Any] = Field(default_factory=dict)
    consent_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class HumanHelpReceipt(ProductContract):
    request_id: str = Field(min_length=1)
    connector: str = Field(min_length=1)
    status: Literal["submitted", "acknowledged", "resolved", "failed", "timed-out"]
    external_request_id: str | None = None
    message: str | None = None
    error: str | None = None


class PerceptionHazard(ProductContract):
    kind: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)


class PerceptionFrameContext(ProductContract):
    schema_version: Literal["perception-context.v1"] = "perception-context.v1"
    context_id: str = Field(min_length=1)
    frame_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_version: str = Field(min_length=1)
    task_provenance: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    checkpoint_epoch: int = Field(ge=0)
    allowed_transitions: list[str] = Field(default_factory=list)
    completion_criteria: str = Field(min_length=1, max_length=1200)
    risk: Literal["low", "medium", "high"]
    source_reference: str = Field(max_length=500)
    authorized_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    frame_captured_at: datetime


class PerceptionHypothesis(ProductContract):
    checkpoint_id: str = Field(min_length=1)
    completed: bool = False
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(max_length=500)
    visible_evidence: list[str] = Field(default_factory=list, max_length=8)
    hazards: list[PerceptionHazard] = Field(default_factory=list, max_length=3)
    unrelated: bool = False


class AmbientFrameContext(ProductContract):
    """Immutable binding for passive observation independent of active tasks.

    Deliberately a separate contract rather than a mode flag on
    PerceptionFrameContext: a distinct ``schema_version`` Literal plus the
    inherited ``extra="forbid"`` means feeding an ambient payload to the
    task-mode validator fails twice over, so no ambient code path can weaken
    the ADR 005 guarantees. See docs/ADR_007_AMBIENT_OBSERVATION.md.
    """

    schema_version: Literal["ambient-context.v1"] = "ambient-context.v1"
    context_id: str = Field(min_length=1)
    frame_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    watchlist_id: str = Field(min_length=1)
    watchlist_version: str = Field(min_length=1)
    watchlist_provenance: str = Field(min_length=1)
    watchlist_checksum: str = Field(min_length=1)
    watch_epoch: int = Field(ge=0)
    watch_activities: list[dict[str, Any]] = Field(max_length=16)
    watch_locations: list[dict[str, Any]] = Field(max_length=32)
    observation_mode: Literal["open", "focused"] = "focused"
    previous_observation: dict[str, Any] | None = None
    reference_context: dict[str, Any] | None = None
    media: dict[str, Any] | None = None
    risk: Literal["low", "medium", "high"] = "low"
    # Required and non-empty: an ambient context cannot be constructed without
    # a recorded consent grant. Opt-in is enforced by the type, not by a
    # runtime check a caller could forget.
    consent_id: str = Field(min_length=1)
    frame_captured_at: datetime


class ObservationReferenceCheck(ProductContract):
    source_ids: list[str] = Field(min_length=1, max_length=8)
    evidence_indices: list[int] = Field(min_length=1, max_length=8)
    relation: Literal["conflict", "change", "uncertain", "consistent"]
    reason: str = Field(min_length=1, max_length=400)


class AmbientObservationHypothesis(ProductContract):
    """A passive observation. Absence is expressed positively, never as an error.

    There is deliberately no ``unrelated`` field and no ``completed`` field: a
    frame showing that no watched activity is happening *is* the observation
    (``observed=False``, ``activity_id="none-observed"``), and ambient mode has
    nothing to complete.
    """

    activity_id: str = Field(min_length=1, max_length=160)
    observed: bool = False
    location_id: str = Field(min_length=1, max_length=160, default="unknown")
    confidence: float = Field(ge=0, le=1, default=0)
    summary: str = Field(max_length=500)
    visible_evidence: list[str] = Field(default_factory=list, max_length=8)
    hazards: list[PerceptionHazard] = Field(default_factory=list, max_length=3)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    clarification: str | None = Field(default=None, max_length=240)
    salient_change: bool = True
    # Optional reasoning metadata is validated separately from visible evidence.
    reference_check: Any = None
    follow_up_of: str | None = Field(default=None, max_length=80)


class ObservationEvidence(ProductContract):
    observation_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    checkpoint_id: str = Field(min_length=1)
    frame_captured_at: datetime
    received_at: datetime
    frame_age_ms: int = Field(ge=0)
    allowed_transitions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    hazards: list[PerceptionHazard] = Field(default_factory=list)
    source: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ProviderDisclosure(ProductContract):
    provider: str = Field(min_length=1)
    data_categories: list[str] = Field(min_length=1)
    purposes: list[str] = Field(min_length=1)
    retention: str = Field(min_length=1)
    user_controls: list[str] = Field(default_factory=list)
