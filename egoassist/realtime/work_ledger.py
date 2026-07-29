"""Durable inbox and job lifecycle primitives for realtime assistance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4
from datetime import datetime, timezone

from .context_harness import SessionJournal


TURN_OPEN_STATUSES = {"received", "deferred", "classifying", "ready", "processing"}
TURN_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
INTENT_STATUSES = {
    "pending", "foreground", "running-background", "suspended",
    "waiting-input", "completed", "failed", "cancelled",
}
JOB_STATUSES = {
    "queued", "running", "suspended", "waiting-input", "waiting-result",
    "completed", "failed", "cancelled",
}
RESULT_STATUSES = {"pending", "displayed", "speaking", "delivered", "dismissed"}


def _latest_by_id(
    entries: list[dict[str, Any]],
    create_type: str,
    state_type: str,
    id_key: str,
) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for entry in entries:
        entry_type = entry.get("type")
        identifier = str(entry.get(id_key, ""))
        if not identifier:
            continue
        if entry_type == create_type:
            values.setdefault(identifier, dict(entry))
        elif entry_type == state_type and identifier in values:
            values[identifier].update({
                "status": entry.get("status"),
                "updatedAt": entry.get("timestamp"),
                "stateEntryId": entry.get("id"),
                "detail": dict(entry.get("detail") or {}),
            })
    return values


class CaseWorkLedger:
    """Three durable ledgers projected from an append-only case journal.

    The journal remains the single writer and recovery source. Inbox and state
    entries contain the complete user text or result, so a crash between later
    projections cannot erase provider-completed work.
    """

    def __init__(self, journal: SessionJournal) -> None:
        self.journal = journal

    def capture_turn(
        self,
        text: str,
        *,
        source: str,
        sequence: int | None = None,
        provider_item_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(text).strip()
        if not normalized:
            raise ValueError("turn text cannot be empty")
        stable_id = str(turn_id or provider_item_id or uuid4().hex[:16])
        entry = self.journal.append(
            "turn_inbox",
            {
                "turnId": stable_id,
                "text": normalized,
                "source": str(source),
                "providerItemId": str(provider_item_id) if provider_item_id else None,
                "sequence": int(sequence) if sequence is not None else None,
                "status": "received",
            },
            idempotency_key=f"turn-inbox:{stable_id}",
        )
        # This second durable projection makes the captured turn immediately
        # available to normal context replay. Recovery can recreate it from the
        # turn_inbox entry if the process stops between these two fsyncs.
        self.ensure_turn_message(stable_id, normalized, source=source)
        return entry

    def ensure_turn_message(self, turn_id: str, text: str, *, source: str) -> dict[str, Any]:
        return self.journal.append_message(
            "user",
            str(text).strip(),
            source=str(source),
            idempotency_key=f"user:{turn_id}",
            metadata={"trust": "untrusted-user-data", "turnId": str(turn_id)},
        )

    def set_turn_status(
        self,
        turn_id: str,
        status: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in TURN_OPEN_STATUSES | TURN_TERMINAL_STATUSES:
            raise ValueError(f"invalid turn status: {status}")
        current = self.turns().get(str(turn_id))
        if current is None:
            raise ValueError(f"unknown turn: {turn_id}")
        if current.get("status") in TURN_TERMINAL_STATUSES and current.get("status") != status:
            raise ValueError(f"terminal turn cannot transition to {status}: {turn_id}")
        return self.journal.append("turn_inbox_state", {
            "turnId": str(turn_id), "status": status, "detail": dict(detail or {}),
        })

    def turns(self) -> dict[str, dict[str, Any]]:
        return _latest_by_id(
            self.journal.entries(), "turn_inbox", "turn_inbox_state", "turnId",
        )

    def recoverable_turns(self) -> list[dict[str, Any]]:
        turns = [item for item in self.turns().values() if item.get("status") in TURN_OPEN_STATUSES]
        for item in turns:
            self.ensure_turn_message(item["turnId"], item["text"], source=item["source"])
        return sorted(
            turns,
            key=lambda item: (
                item.get("sequence") is None,
                item.get("sequence") if item.get("sequence") is not None else 0,
                item.get("timestamp", ""),
            ),
        )

    def create_intent(
        self,
        turn_id: str,
        *,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        skill_ref: Mapping[str, Any] | None = None,
        status: str = "pending",
        intent_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in INTENT_STATUSES:
            raise ValueError(f"invalid intent status: {status}")
        if str(turn_id) not in self.turns():
            raise ValueError(f"unknown turn: {turn_id}")
        stable_id = str(intent_id or uuid4().hex[:16])
        return self.journal.append("intent_envelope", {
            "intentId": stable_id,
            "turnId": str(turn_id),
            "kind": str(kind),
            "payload": dict(payload or {}),
            "skillRef": dict(skill_ref or {}),
            "status": status,
        }, idempotency_key=f"intent:{stable_id}")

    def set_intent_status(
        self,
        intent_id: str,
        status: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in INTENT_STATUSES:
            raise ValueError(f"invalid intent status: {status}")
        if str(intent_id) not in self.intents():
            raise ValueError(f"unknown intent: {intent_id}")
        return self.journal.append("intent_state", {
            "intentId": str(intent_id), "status": status, "detail": dict(detail or {}),
        })

    def intents(self) -> dict[str, dict[str, Any]]:
        values = _latest_by_id(
            self.journal.entries(), "intent_envelope", "intent_state", "intentId",
        )
        for item in values.values():
            initial_turn = str(item.get("turnId", ""))
            if initial_turn:
                item["turnIds"] = [initial_turn]
        for entry in self.journal.entries():
            if entry.get("type") != "intent_turn_link":
                continue
            intent_id = str(entry.get("intentId", ""))
            turn_id = str(entry.get("turnId", ""))
            if intent_id in values and turn_id:
                values[intent_id].setdefault("turnIds", [])
                if turn_id not in values[intent_id]["turnIds"]:
                    values[intent_id]["turnIds"].append(turn_id)
        return values

    def link_turn_to_intent(
        self,
        intent_id: str,
        turn_id: str,
        *,
        relation: str = "continuation",
    ) -> dict[str, Any]:
        if str(intent_id) not in self.intents():
            raise ValueError(f"unknown intent: {intent_id}")
        if str(turn_id) not in self.turns():
            raise ValueError(f"unknown turn: {turn_id}")
        return self.journal.append(
            "intent_turn_link",
            {
                "intentId": str(intent_id),
                "turnId": str(turn_id),
                "relation": str(relation),
            },
            idempotency_key=f"intent-turn:{intent_id}:{turn_id}",
        )

    def focus_intent(self, intent_id: str | None, *, reason: str) -> dict[str, Any]:
        if intent_id is not None and str(intent_id) not in self.intents():
            raise ValueError(f"unknown intent: {intent_id}")
        previous_id = self.foreground_intent_id()
        requested_id = str(intent_id) if intent_id is not None else None
        if previous_id and previous_id != requested_id:
            previous = self.intents().get(previous_id)
            if previous and previous.get("status") == "foreground":
                self.set_intent_status(
                    previous_id, "suspended", detail={"reason": "foreground-focus-changed"},
                )
        return self.journal.append("foreground_focus", {
            "intentId": str(intent_id) if intent_id is not None else None,
            "reason": str(reason),
        })

    def foreground_intent_id(self) -> str | None:
        return next((
            str(entry["intentId"]) if entry.get("intentId") is not None else None
            for entry in reversed(self.journal.entries())
            if entry.get("type") == "foreground_focus"
        ), None)

    def intent_runtime_snapshot(self, intent_id: str) -> dict[str, Any] | None:
        for entry in reversed(self.journal.entries()):
            if entry.get("type") != "intent_state" or str(entry.get("intentId", "")) != str(intent_id):
                continue
            snapshot = (entry.get("detail") or {}).get("runtimeCheckpoint")
            if isinstance(snapshot, dict):
                return dict(snapshot)
        return None

    def create_job(
        self,
        intent_id: str,
        *,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        status: str = "queued",
        job_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in JOB_STATUSES:
            raise ValueError(f"invalid job status: {status}")
        if str(intent_id) not in self.intents():
            raise ValueError(f"unknown intent: {intent_id}")
        stable_id = str(job_id or uuid4().hex[:16])
        return self.journal.append("job_ledger", {
            "jobId": stable_id,
            "intentId": str(intent_id),
            "kind": str(kind),
            "payload": dict(payload or {}),
            "status": status,
        }, idempotency_key=f"job:{stable_id}")

    def set_job_status(
        self,
        job_id: str,
        status: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in JOB_STATUSES:
            raise ValueError(f"invalid job status: {status}")
        if str(job_id) not in self.jobs():
            raise ValueError(f"unknown job: {job_id}")
        return self.journal.append("job_state", {
            "jobId": str(job_id), "status": status, "detail": dict(detail or {}),
        })

    def jobs(self) -> dict[str, dict[str, Any]]:
        return _latest_by_id(self.journal.entries(), "job_ledger", "job_state", "jobId")

    def cancel_job(self, job_id: str, *, reason: str = "explicit-user-cancel") -> dict[str, Any]:
        job = self.jobs().get(str(job_id))
        if job is None:
            raise ValueError(f"unknown job: {job_id}")
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return job
        self.set_job_status(job_id, "cancelled", detail={"reason": str(reason)})
        intent_id = str(job.get("intentId", ""))
        remaining = [
            item for item in self.jobs().values()
            if str(item.get("intentId", "")) == intent_id
            and item.get("jobId") != str(job_id)
            and item.get("status") not in {"completed", "failed", "cancelled"}
        ]
        if intent_id and not remaining and intent_id in self.intents():
            self.set_intent_status(intent_id, "cancelled", detail={"reason": str(reason)})
        return self.jobs()[str(job_id)]

    def due_jobs(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        due: list[dict[str, Any]] = []
        for job in self.jobs().values():
            if job.get("status") not in {"queued", "running", "waiting-result"}:
                continue
            raw_due = (job.get("payload") or {}).get("dueAt")
            if not raw_due:
                continue
            try:
                due_at = datetime.fromisoformat(str(raw_due).replace("Z", "+00:00"))
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if due_at <= current:
                due.append(job)
        return due

    def enqueue_result(
        self,
        intent_id: str,
        content: Any,
        *,
        job_id: str | None = None,
        priority: int = 50,
        delivery: str = "speech-or-display",
        result_id: str | None = None,
    ) -> dict[str, Any]:
        if str(intent_id) not in self.intents():
            raise ValueError(f"unknown intent: {intent_id}")
        if job_id is not None and str(job_id) not in self.jobs():
            raise ValueError(f"unknown job: {job_id}")
        stable_id = str(result_id or uuid4().hex[:16])
        return self.journal.append("result_inbox", {
            "resultId": stable_id,
            "intentId": str(intent_id),
            "jobId": str(job_id) if job_id is not None else None,
            "content": content,
            "priority": int(priority),
            "delivery": str(delivery),
            "status": "pending",
        }, idempotency_key=f"result:{stable_id}")

    def set_result_status(
        self,
        result_id: str,
        status: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in RESULT_STATUSES:
            raise ValueError(f"invalid result status: {status}")
        if str(result_id) not in self.results():
            raise ValueError(f"unknown result: {result_id}")
        return self.journal.append("result_delivery", {
            "resultId": str(result_id), "status": status, "detail": dict(detail or {}),
        })

    def results(self) -> dict[str, dict[str, Any]]:
        return _latest_by_id(
            self.journal.entries(), "result_inbox", "result_delivery", "resultId",
        )

    def pending_results(self) -> list[dict[str, Any]]:
        return sorted(
            (item for item in self.results().values() if item.get("status") == "pending"),
            key=lambda item: (-int(item.get("priority", 0)), item.get("timestamp", "")),
        )

    def claim_next_result(self, *, delivery: str = "speech-or-display") -> dict[str, Any] | None:
        """Claim one result for the single output arbiter without losing others."""
        for item in self.pending_results():
            if delivery and str(item.get("delivery")) != delivery:
                continue
            self.set_result_status(item["resultId"], "speaking", detail={"arbiter": "single-writer"})
            return self.results().get(item["resultId"])
        return None

    def recover_delivery_claims(self) -> int:
        """Return abandoned single-writer claims to the durable inbox on reconnect."""
        recovered = 0
        for item in self.results().values():
            if item.get("status") == "speaking":
                self.set_result_status(
                    item["resultId"],
                    "pending",
                    detail={"reason": "connection-recovered-before-delivery"},
                )
                recovered += 1
        return recovered

    def snapshot(self) -> dict[str, Any]:
        return {
            "turnInbox": list(self.turns().values()),
            "intents": list(self.intents().values()),
            "foregroundIntentId": self.foreground_intent_id(),
            "jobs": list(self.jobs().values()),
            "resultInbox": list(self.results().values()),
        }
