"""SQLAlchemy database access and a lossless legacy journal projector."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from egoassist.realtime.context_harness import SessionJournal

from .models import (
    Base,
    ConversationTurnRow,
    IntentRow,
    JobRow,
    ResultRow,
    UserRow,
)


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

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_schema(self) -> None:
        Base.metadata.drop_all(self.engine)

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

    def project_journal(
        self,
        journal: SessionJournal | str | Path,
        *,
        user_id: str,
        display_name: str | None = None,
    ) -> dict[str, int]:
        """Project old JSONL state without mutating it or dropping memory data."""
        self.create_schema()
        source = journal if isinstance(journal, SessionJournal) else SessionJournal(journal)
        entries = source.entries()
        counts = {"turns": 0, "intents": 0, "jobs": 0, "results": 0}
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
