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


class IntentEnvelope(ProductContract):
    intent_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    status: Literal["pending", "foreground", "running-background", "suspended", "waiting-input", "completed", "failed", "cancelled"] = "pending"
    payload: dict[str, Any] = Field(default_factory=dict)
    skill_ref: dict[str, Any] = Field(default_factory=dict)
    correlation: CorrelationIds = Field(default_factory=CorrelationIds)


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


class JobAttempt(ProductContract):
    attempt_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    status: Literal["leased", "running", "succeeded", "failed", "cancelled"] = "leased"
    connector_request_id: str | None = None
    error: str | None = None
    retry_class: str | None = None
    committed_external_effect: bool = False
    started_at: datetime | None = None
    ended_at: datetime | None = None
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
    hazards: list[str] = Field(default_factory=list)
    source: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ProviderDisclosure(ProductContract):
    provider: str = Field(min_length=1)
    data_categories: list[str] = Field(min_length=1)
    purposes: list[str] = Field(min_length=1)
    retention: str = Field(min_length=1)
    user_controls: list[str] = Field(default_factory=list)
