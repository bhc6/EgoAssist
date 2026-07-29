"""SQLAlchemy product persistence models.

These tables are projections of typed product contracts. Legacy JSONL journals
and the EgoAssist TimelineMemory database remain readable during migration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceRow(Base):
    __tablename__ = "devices"
    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConsentGrantRow(Base):
    __tablename__ = "consent_grants"
    consent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    data_categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    destination: Mapped[str] = mapped_column(String(300), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RetentionPolicyRow(Base):
    __tablename__ = "retention_policies"
    policy_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    deletion_mode: Mapped[str] = mapped_column(String(32), default="hard-delete", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_retention_user_category"),)


class ConversationTurnRow(Base):
    __tablename__ = "conversation_turns"
    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_item_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    language: Mapped[str | None] = mapped_column(String(32))
    retention_class: Mapped[str] = mapped_column(String(64), default="transcript", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="received", nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    correlation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class IntentRow(Base):
    __tablename__ = "intents"
    intent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("conversation_turns.turn_id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    skill_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    correlation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class IntentRelationRow(Base):
    __tablename__ = "intent_relations"
    relation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_intent_id: Mapped[str] = mapped_column(ForeignKey("intents.intent_id"), nullable=False)
    target_intent_id: Mapped[str] = mapped_column(ForeignKey("intents.intent_id"), nullable=False)
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class TaskPlanRow(Base):
    __tablename__ = "task_plans"
    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("intents.intent_id"), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRow(Base):
    __tablename__ = "jobs"
    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("intents.intent_id"), nullable=False, index=True)
    workflow_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    executor_kind: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_policy: Mapped[str] = mapped_column(String(64), default="explicit-only", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    correlation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class JobAttemptRow(Base):
    __tablename__ = "job_attempts"
    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="leased", nullable=False)
    connector_request_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    retry_class: Mapped[str | None] = mapped_column(String(64))
    committed_external_effect: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    artifact_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(200))
    step_id: Mapped[str | None] = mapped_column(String(128))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ResultRow(Base):
    __tablename__ = "results"
    result_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("intents.intent_id"), nullable=False, index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.job_id"))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    content: Mapped[Any] = mapped_column(JSON, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    delivery: Mapped[str] = mapped_column(String(64), default="speech-or-display", nullable=False)
    correlation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class DeliveryAttemptRow(Base):
    __tablename__ = "delivery_attempts"
    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    result_id: Mapped[str] = mapped_column(ForeignKey("results.result_id"), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(240), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class MemoryCardRow(Base):
    __tablename__ = "memory_cards"
    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    case_id: Mapped[str | None] = mapped_column(String(128), index=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MemoryDraftRow(Base):
    __tablename__ = "memory_drafts"
    draft_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ObservationRow(Base):
    __tablename__ = "observations"
    observation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    frame_captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    frame_age_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    allowed_transitions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    hazards: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.user_id"), index=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    correlation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    payload_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retention_class: Mapped[str] = mapped_column(String(64), default="operational", nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
