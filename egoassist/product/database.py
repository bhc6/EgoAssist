"""SQLAlchemy database access and a lossless legacy journal projector."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import threading
from typing import Any, Iterator

from sqlalchemy import create_engine, select, delete as sql_delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from egoassist.core.journal import SessionJournal

from .models import (
    Base,
    ConversationTurnRow,
    IntentRow,
    JobRow,
    ObservationRow,
    ResultRow,
    UserRow,
    DeviceRow, ConsentGrantRow, RetentionPolicyRow, IntentRelationRow, TaskPlanRow,
    JobAttemptRow, ArtifactRow, DeliveryAttemptRow, MemoryCardRow, MemoryDraftRow,
    AuditEventRow,
)
from egoassist.core.contracts import ObservationEvidence


def _timestamp(value: Any) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class ProductDatabase:
    """Small product DB facade; callers never need to know SQLAlchemy details."""

    def __init__(self, url_or_path: str | Path) -> None:
        raw = str(url_or_path)
        if "://" not in raw:
            path = Path(raw)
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = f"sqlite:///{path.as_posix()}"
        connect_args = {"check_same_thread": False} if raw.startswith("sqlite") else {}
        self.engine: Engine = create_engine(raw, future=True, connect_args=connect_args)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, class_=Session)
        self._projection_lock = threading.RLock()
        self._journal_projections: dict[tuple[str, str, str], tuple[Any, dict[str, int]]] = {}

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        """Release pooled connections when the runtime root is shut down."""
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ensure_user(self, user_id: str, *, display_name: str | None = None) -> None:
        with self.session() as session:
            row = session.get(UserRow, str(user_id))
            if row is None:
                session.add(UserRow(user_id=str(user_id), display_name=display_name or str(user_id)))

    @staticmethod
    def _row_payload(row: Any) -> dict[str, Any]:
        """Return JSON-safe column data without exposing ORM internals."""
        from sqlalchemy import inspect
        result: dict[str, Any] = {}
        for column in inspect(row).mapper.column_attrs:
            value = getattr(row, column.key)
            if isinstance(value, datetime):
                value = value.isoformat()
            result[column.key] = value
        return result

    def export_user(self, user_id: str) -> dict[str, Any]:
        """Export every product-owned record for one account, preserving scope/provenance."""
        self.create_schema()
        user_id = str(user_id)
        tables = (
            (UserRow, "users", lambda row: row.user_id == user_id),
            (DeviceRow, "devices", lambda row: row.user_id == user_id),
            (ConsentGrantRow, "consentGrants", lambda row: row.user_id == user_id),
            (RetentionPolicyRow, "retentionPolicies", lambda row: row.user_id == user_id),
            (ConversationTurnRow, "conversationTurns", lambda row: row.user_id == user_id),
            (IntentRow, "intents", lambda row: row.user_id == user_id),
            (IntentRelationRow, "intentRelations", lambda row: row.source_intent_id.in_(select(IntentRow.intent_id).where(IntentRow.user_id == user_id)) | row.target_intent_id.in_(select(IntentRow.intent_id).where(IntentRow.user_id == user_id))),
            (TaskPlanRow, "taskPlans", lambda row: row.intent_id.in_(select(IntentRow.intent_id).where(IntentRow.user_id == user_id))),
            (JobRow, "jobs", lambda row: row.intent_id.in_(select(IntentRow.intent_id).where(IntentRow.user_id == user_id))),
            (JobAttemptRow, "jobAttempts", lambda row: row.job_id.in_(select(JobRow.job_id).where(JobRow.intent_id.in_(select(IntentRow.intent_id).where(IntentRow.user_id == user_id))))),
            (ArtifactRow, "artifacts", lambda row: row.owner_id == user_id),
            (ResultRow, "results", lambda row: row.intent_id.in_(select(IntentRow.intent_id).where(IntentRow.user_id == user_id))),
            (DeliveryAttemptRow, "deliveryAttempts", lambda row: row.result_id.in_(select(ResultRow.result_id).where(ResultRow.intent_id.in_(select(IntentRow.intent_id).where(IntentRow.user_id == user_id))))),
            (MemoryCardRow, "memoryCards", lambda row: row.user_id == user_id),
            (MemoryDraftRow, "memoryDrafts", lambda row: row.user_id == user_id),
            (ObservationRow, "observations", lambda row: row.user_id == user_id),
            (AuditEventRow, "auditEvents", lambda row: row.user_id == user_id),
        )
        with self.session() as session:
            payload = {"schema": "egoassist.account-export.v1", "userId": user_id, "exportedAt": datetime.now(timezone.utc).isoformat(), "data": {}}
            for model, name, predicate in tables:
                payload["data"][name] = [self._row_payload(item) for item in session.scalars(select(model).where(predicate(model)))]
            return payload

    def delete_user(self, user_id: str) -> dict[str, int]:
        """Hard-delete product projections for an account; safe to repeat."""
        self.create_schema()
        user_id = str(user_id)
        counts: dict[str, int] = {}
        with self.session() as session:
            intent_ids = [row[0] for row in session.execute(select(IntentRow.intent_id).where(IntentRow.user_id == user_id))]
            job_ids = [row[0] for row in session.execute(select(JobRow.job_id).where(JobRow.intent_id.in_(intent_ids or ["__none__"]))) ]
            result_ids = [row[0] for row in session.execute(select(ResultRow.result_id).where(ResultRow.intent_id.in_(intent_ids or ["__none__"]))) ]
            order = [
                (DeliveryAttemptRow, DeliveryAttemptRow.result_id.in_(result_ids or ["__none__"])),
                (JobAttemptRow, JobAttemptRow.job_id.in_(job_ids or ["__none__"])),
                (TaskPlanRow, TaskPlanRow.intent_id.in_(intent_ids or ["__none__"])),
                (IntentRelationRow, (IntentRelationRow.source_intent_id.in_(intent_ids or ["__none__"]) | IntentRelationRow.target_intent_id.in_(intent_ids or ["__none__"]))),
                (ResultRow, ResultRow.result_id.in_(result_ids or ["__none__"])),
                (JobRow, JobRow.job_id.in_(job_ids or ["__none__"])),
                (IntentRow, IntentRow.intent_id.in_(intent_ids or ["__none__"])),
                (ConversationTurnRow, ConversationTurnRow.user_id == user_id),
                (DeviceRow, DeviceRow.user_id == user_id),
                (ConsentGrantRow, ConsentGrantRow.user_id == user_id),
                (RetentionPolicyRow, RetentionPolicyRow.user_id == user_id),
                (ArtifactRow, ArtifactRow.owner_id == user_id),
                (MemoryCardRow, MemoryCardRow.user_id == user_id),
                (MemoryDraftRow, MemoryDraftRow.user_id == user_id),
                (ObservationRow, ObservationRow.user_id == user_id),
                (AuditEventRow, AuditEventRow.user_id == user_id),
                (UserRow, UserRow.user_id == user_id),
            ]
            for model, criterion in order:
                result = session.execute(sql_delete(model).where(criterion))
                counts[model.__tablename__] = counts.get(model.__tablename__, 0) + int(result.rowcount or 0)
        with self._projection_lock:
            self._journal_projections = {
                key: value for key, value in self._journal_projections.items() if key[2] != user_id
            }
        return counts

    def set_retention_policy(self, user_id: str, category: str, retention_days: int, *, deletion_mode: str = "hard-delete") -> dict[str, Any]:
        if category not in {"transcript", "observation", "operational", "memory"} or deletion_mode != "hard-delete":
            raise ValueError("unsupported retention policy")
        if int(retention_days) < 0 or int(retention_days) > 36500:
            raise ValueError("retention_days must be between 0 and 36500")
        self.ensure_user(str(user_id))
        policy_id = f"retention:{user_id}:{category}"
        with self.session() as session:
            row = session.get(RetentionPolicyRow, policy_id)
            if row is None:
                row = RetentionPolicyRow(policy_id=policy_id, user_id=str(user_id), category=str(category), retention_days=int(retention_days), deletion_mode=str(deletion_mode))
                session.add(row)
            else:
                row.retention_days = int(retention_days); row.deletion_mode = str(deletion_mode)
            return self._row_payload(row)

    def retention_policies(self, user_id: str | None = None) -> list[dict[str, Any]]:
        with self.session() as session:
            return [self._row_payload(row) for row in session.scalars(select(RetentionPolicyRow).where(RetentionPolicyRow.user_id == str(user_id) if user_id else True).order_by(RetentionPolicyRow.category))]

    def forget_turns(self, user_id, turn_ids):
        """Remove dependent projections before their source turn disappears."""
        if not turn_ids:
            return
        with self.session() as session:
            intents = select(IntentRow.intent_id).where(IntentRow.user_id == user_id, IntentRow.turn_id.in_(turn_ids))
            jobs = select(JobRow.job_id).where(JobRow.intent_id.in_(intents))
            results = select(ResultRow.result_id).where(ResultRow.intent_id.in_(intents))
            for model, criterion in (
                (DeliveryAttemptRow, DeliveryAttemptRow.result_id.in_(results)),
                (JobAttemptRow, JobAttemptRow.job_id.in_(jobs)),
                (TaskPlanRow, TaskPlanRow.intent_id.in_(intents)),
                (IntentRelationRow, IntentRelationRow.source_intent_id.in_(intents) | IntentRelationRow.target_intent_id.in_(intents)),
                (ResultRow, ResultRow.intent_id.in_(intents)),
                (JobRow, JobRow.intent_id.in_(intents)),
                (IntentRow, IntentRow.intent_id.in_(intents)),
                (ConversationTurnRow, (ConversationTurnRow.user_id == user_id) & ConversationTurnRow.turn_id.in_(turn_ids)),
            ):
                session.execute(sql_delete(model).where(criterion), execution_options={"synchronize_session": False})

    def apply_retention(self, *, now: datetime | None = None, user_id: str | None = None) -> dict[str, Any]:
        """Apply configured policies and return bounded deletion evidence."""
        now = now or datetime.now(timezone.utc)
        users = [str(user_id)] if user_id else None
        deleted: dict[str, int] = {}
        with self.session() as session:
            policies = list(session.scalars(select(RetentionPolicyRow).where(RetentionPolicyRow.user_id.in_(users) if users else True)))
            for policy in policies:
                cutoff = now - timedelta(days=int(policy.retention_days))
                category = policy.category
                model_column = {
                    "transcript": (ConversationTurnRow, ConversationTurnRow.recorded_at),
                    "observation": (ObservationRow, ObservationRow.received_at),
                    "operational": (AuditEventRow, AuditEventRow.recorded_at),
                }.get(category)
                if model_column is None:
                    continue
                model, column = model_column
                criterion = (column < cutoff) & (model.user_id == policy.user_id)
                result = session.execute(sql_delete(model).where(criterion))
                deleted[category] = deleted.get(category, 0) + int(result.rowcount or 0)
        return {"appliedAt": now.isoformat(), "deleted": deleted, "evidence": "counts-only"}

    def record_consent_grant(
        self,
        user_id: str,
        *,
        purpose: str,
        data_categories: list[str],
        destination: str,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record one purpose-scoped consent grant idempotently.

        The consent id is deterministic, so re-granting the same purpose
        refreshes the existing row rather than accumulating duplicates.
        """
        purpose_value = str(purpose).strip()
        if not purpose_value:
            raise ValueError("consent purpose is required")
        if not data_categories:
            raise ValueError("consent requires at least one data category")
        self.ensure_user(str(user_id))
        consent_id = f"consent:{user_id}:{purpose_value}"
        granted = now or datetime.now(timezone.utc)
        with self.session() as session:
            row = session.get(ConsentGrantRow, consent_id)
            if row is None:
                row = ConsentGrantRow(
                    consent_id=consent_id,
                    user_id=str(user_id),
                    purpose=purpose_value,
                    data_categories=[str(item) for item in data_categories],
                    destination=str(destination),
                    granted_at=granted,
                    expires_at=expires_at,
                )
                session.add(row)
            else:
                row.data_categories = [str(item) for item in data_categories]
                row.destination = str(destination)
                row.granted_at = granted
                row.expires_at = expires_at
                row.revoked_at = None
            return self._row_payload(row)

    def active_consent(
        self, user_id: str, *, purpose: str, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Return the live grant for a purpose, or None when absent, revoked, or expired."""
        moment = now or datetime.now(timezone.utc)
        with self.session() as session:
            row = session.get(ConsentGrantRow, f"consent:{user_id}:{str(purpose).strip()}")
            if row is None or row.revoked_at is not None:
                return None
            if row.expires_at is not None and _timestamp(row.expires_at) <= moment:
                return None
            return self._row_payload(row)

    def revoke_consent(self, consent_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.get(ConsentGrantRow, str(consent_id))
            if row is None:
                return None
            if row.revoked_at is None:
                row.revoked_at = now or datetime.now(timezone.utc)
            return self._row_payload(row)

    def record_observation(self, value: ObservationEvidence) -> None:
        """Persist one validated perception artifact idempotently."""
        self.create_schema()
        with self.session() as session:
            user = session.get(UserRow, value.user_id)
            if user is None:
                session.add(UserRow(user_id=value.user_id, display_name=value.user_id))
                session.flush()
            if session.get(ObservationRow, value.observation_id) is not None:
                return
            session.add(ObservationRow(
                observation_id=value.observation_id,
                user_id=value.user_id,
                session_id=value.session_id,
                task_id=value.task_id,
                checkpoint_id=value.checkpoint_id,
                frame_captured_at=value.frame_captured_at,
                received_at=value.received_at,
                frame_age_ms=value.frame_age_ms,
                allowed_transitions=list(value.allowed_transitions),
                confidence=value.confidence,
                hazards=[item.model_dump(mode="json") for item in value.hazards],
                source=value.source,
                evidence=dict(value.evidence),
            ))

    def observations(
        self,
        user_id: str,
        *,
        task_id: str | None = None,
        since: datetime | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Read stored observations oldest-first, optionally scoped to one task id."""
        with self.session() as session:
            query = select(ObservationRow).where(ObservationRow.user_id == str(user_id))
            if task_id is not None:
                query = query.where(ObservationRow.task_id == str(task_id))
            if since is not None:
                query = query.where(ObservationRow.frame_captured_at >= since)
            query = query.order_by(ObservationRow.frame_captured_at).limit(max(1, int(limit)))
            return [self._row_payload(row) for row in session.scalars(query)]

    def project_journal(
        self,
        journal: SessionJournal | str | Path,
        *,
        user_id: str,
        display_name: str | None = None,
    ) -> dict[str, int]:
        """Replay only new events; advance the cursor only after the DB commit.

        Cursors are process-local. A restart safely replays the retained journal
        once, using the existing idempotent rows to rebuild the projection.
        """
        source = journal if isinstance(journal, SessionJournal) else SessionJournal(journal)
        key = (str(source.path.resolve()), source.session_id, str(user_id))
        with self._projection_lock:
            cursor, previous = self._journal_projections.get(key, (None, {"turns": 0, "intents": 0, "jobs": 0, "results": 0}))
            next_cursor, entries, reset = source.read_since(cursor, types={
                "turn_inbox", "turn_inbox_state", "intent_envelope", "intent_state",
                "job_ledger", "job_state", "result_inbox", "result_delivery", "retention.deleted",
            })
            if reset:
                previous = {"turns": 0, "intents": 0, "jobs": 0, "results": 0}
            if cursor and not reset and not entries:
                # Filtered media rows need no database write, but must not be
                # scanned again on every projection poll.
                self._journal_projections[key] = (next_cursor, previous)
                return dict(previous)
            if cursor is None or reset:
                self.create_schema()
                self.ensure_user(str(user_id), display_name=display_name)
            cutoffs = {p["category"]: datetime.now(timezone.utc) - timedelta(days=p["retention_days"])
                       for p in self.retention_policies(str(user_id))}
            erased_turns = {turn for row in entries if row.get("type") == "retention.deleted" for turn in row.get("turnIds", [])}
            expired_turns = {row["turnId"] for row in entries if row.get("type") == "turn_inbox"
                             and cutoffs.get("transcript") and _timestamp(row.get("timestamp")) < cutoffs["transcript"]}
            self.forget_turns(str(user_id), erased_turns | expired_turns)
            entries = [row for row in entries if row.get("turnId") not in erased_turns | expired_turns]
            counts = self._project_entries(source, entries, str(user_id), display_name)
            totals = {name: previous[name] + counts[name] for name in previous}
            self._journal_projections[key] = (next_cursor, totals)
            return dict(totals)

    def _project_entries(
        self, source: SessionJournal, entries: list[dict[str, Any]],
        user_id: str, display_name: str | None,
    ) -> dict[str, int]:
        counts = {"turns": 0, "intents": 0, "jobs": 0, "results": 0}
        projected_types = {"turn_inbox", "turn_inbox_state", "intent_envelope", "intent_state",
                           "job_ledger", "job_state", "result_inbox", "result_delivery"}
        entries = [entry for entry in entries if entry.get("type") in projected_types]
        if not entries:
            return counts
        with self.session() as session:
            user = session.get(UserRow, str(user_id))
            if user is None:
                session.add(UserRow(user_id=str(user_id), display_name=display_name or str(user_id)))
                session.flush()
            for entry in entries:
                entry_type = entry.get("type")
                if entry_type == "turn_inbox":
                    turn_id = str(entry.get("turnId", ""))
                    if not turn_id:
                        continue
                    row = session.get(ConversationTurnRow, turn_id)
                    payload = entry
                    if row is None:
                        row = ConversationTurnRow(
                            turn_id=turn_id,
                            user_id=str(user_id),
                            session_id=source.session_id,
                            text=str(payload.get("text", "")),
                            source=str(payload.get("source", "legacy")),
                            provider_item_id=payload.get("providerItemId"),
                            status=str(payload.get("status", "received")),
                            recorded_at=_timestamp(entry.get("timestamp")),
                            provenance={"legacyJournal": str(source.path)},
                            correlation={"turnId": turn_id},
                        )
                        session.add(row)
                    counts["turns"] += 1
                elif entry_type == "turn_inbox_state":
                    row = session.get(ConversationTurnRow, str(entry.get("turnId", "")))
                    if row is not None:
                        row.status = str(entry.get("status", row.status))
                elif entry_type == "intent_envelope":
                    intent_id = str(entry.get("intentId", ""))
                    if not intent_id:
                        continue
                    row = session.get(IntentRow, intent_id)
                    if row is None:
                        row = IntentRow(
                            intent_id=intent_id,
                            turn_id=str(entry.get("turnId", "")),
                            user_id=str(user_id),
                            kind=str(entry.get("kind", "foreground-dialogue")),
                            status=str(entry.get("status", "pending")),
                            payload=dict(entry.get("payload") or {}),
                            skill_ref=dict(entry.get("skillRef") or {}),
                            correlation={"turnId": str(entry.get("turnId", "")), "intentId": intent_id},
                            created_at=_timestamp(entry.get("timestamp")),
                        )
                        session.add(row)
                    counts["intents"] += 1
                elif entry_type == "intent_state":
                    row = session.get(IntentRow, str(entry.get("intentId", "")))
                    if row is not None:
                        row.status = str(entry.get("status", row.status))
                elif entry_type == "job_ledger":
                    job_id = str(entry.get("jobId", ""))
                    if not job_id:
                        continue
                    row = session.get(JobRow, job_id)
                    if row is None:
                        payload = dict(entry.get("payload") or {})
                        row = JobRow(
                            job_id=job_id,
                            intent_id=str(entry.get("intentId", "")),
                            workflow_id=payload.get("workflowId"),
                            executor_kind=str(entry.get("kind", "legacy")),
                            idempotency_key=f"legacy-job:{job_id}",
                            status=str(entry.get("status", "queued")),
                            deadline=_timestamp(payload["dueAt"]) if payload.get("dueAt") else None,
                            payload=payload,
                            correlation={"intentId": str(entry.get("intentId", "")), "jobId": job_id},
                            created_at=_timestamp(entry.get("timestamp")),
                        )
                        session.add(row)
                    counts["jobs"] += 1
                elif entry_type == "job_state":
                    row = session.get(JobRow, str(entry.get("jobId", "")))
                    if row is not None:
                        row.status = str(entry.get("status", row.status))
                elif entry_type == "result_inbox":
                    result_id = str(entry.get("resultId", ""))
                    if not result_id:
                        continue
                    row = session.get(ResultRow, result_id)
                    if row is None:
                        row = ResultRow(
                            result_id=result_id,
                            intent_id=str(entry.get("intentId", "")),
                            job_id=entry.get("jobId"),
                            status=str(entry.get("status", "pending")),
                            content=entry.get("content"),
                            priority=int(entry.get("priority", 50)),
                            delivery=str(entry.get("delivery", "speech-or-display")),
                            correlation={
                                "intentId": str(entry.get("intentId", "")),
                                "jobId": entry.get("jobId"),
                                "resultId": result_id,
                            },
                            provenance={"legacyJournal": str(source.path)},
                            created_at=_timestamp(entry.get("timestamp")),
                        )
                        session.add(row)
                    counts["results"] += 1
                elif entry_type == "result_delivery":
                    row = session.get(ResultRow, str(entry.get("resultId", "")))
                    if row is not None:
                        row.status = str(entry.get("status", row.status))
        return counts

    def table_counts(self) -> dict[str, int]:
        from sqlalchemy import func, select

        with self.session() as session:
            return {
                name: int(session.scalar(select(func.count()).select_from(model)) or 0)
                for name, model in {
                    "users": UserRow,
                    "turns": ConversationTurnRow,
                    "intents": IntentRow,
                    "jobs": JobRow,
                    "results": ResultRow,
                }.items()
            }
