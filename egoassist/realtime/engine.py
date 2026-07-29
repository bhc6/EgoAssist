"""Task preference compatibility view over the shared timeline memory."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from egoassist.memory.timeline import TimelineMemory


class PreferenceMemory:
    """Store confirmed task/checkpoint preferences as typed timeline cards."""

    def __init__(
        self,
        memory_path: str | Path = ".egoassist/runtime.json",
        event_sink: Callable[[str, str, dict[str, Any]], Any] | None = None,
        *,
        timeline: TimelineMemory | None = None,
        user_id: str = "local-default",
        case_id: str | None = None,
        scope: str = "participant",
    ) -> None:
        legacy_path = Path(memory_path)
        self._timeline = timeline or TimelineMemory(
            legacy_path.with_suffix(legacy_path.suffix + ".sqlite3"),
            event_sink=event_sink,
        )
        self.memory_path = self._timeline.path
        self.user_id = str(user_id)
        self.case_id = str(case_id) if case_id else None
        self.scope = str(scope)
        if self.scope not in {"case", "participant"}:
            raise ValueError("invalid preference memory scope")
        if self.scope == "case" and not self.case_id:
            self.case_id = "local-default"
        self._lock = threading.RLock()
        self.session_number = 1
        self.session_events: list[dict[str, Any]] = []
        self.last_decision = self._decision("silent", "等待事件", "尚无需要介入的证据。")
        self._scoped_events: dict[str, list[dict[str, Any]]] = {}
        self._scoped_decisions: dict[str, dict[str, Any]] = {}
        self._event_sink = event_sink
        if timeline is None:
            self.import_legacy(legacy_path)

    @staticmethod
    def _candidate_key(candidate_scope: str | None) -> str:
        return str(candidate_scope or "__default__")

    @property
    def candidate(self) -> dict[str, Any] | None:
        return self._candidate(None)

    def snapshot(self, *, candidate_scope: str | None = None) -> dict[str, Any]:
        with self._lock:
            candidate_key = self._candidate_key(candidate_scope)
            scoped = candidate_scope is not None
            return {
                "session": self.session_number,
                "memoryPath": str(self.memory_path),
                "records": self._records(),
                "candidate": self._candidate(candidate_scope),
                "decision": (
                    self._scoped_decisions.get(
                        candidate_key,
                        self._decision("silent", "等待事件", "尚无需要介入的证据。"),
                    ) if scoped else self.last_decision
                ),
                "events": list(reversed(
                    (self._scoped_events.get(candidate_key, []) if scoped else self.session_events)[-6:]
                )),
            }

    def resolve_candidate(
        self,
        accepted: bool,
        *,
        candidate_scope: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            candidate = self._candidate(candidate_scope)
            if candidate is None:
                raise ValueError("no pending memory candidate")
            if accepted:
                card = self._timeline.activate_pending(candidate["id"])
                record = self._record(card)
                self._set_decision(
                    self._decision(
                        "saved", "已保存", "这条记忆将在后续相同步骤中复用，可随时删除。",
                        evidence_id=record["id"],
                    ),
                    candidate_scope,
                )
                self._emit("memory.confirmed", {
                    "memoryId": record["id"],
                    "trigger": record["trigger"],
                    "preference": record["preference"],
                }, candidate_scope)
            else:
                self._timeline.update_status(candidate["id"], "deleted")
                self._set_decision(
                    self._decision("silent", "未保存", "候选偏好已丢弃，不影响未来会话。"),
                    candidate_scope,
                )
                self._emit("memory.rejected", {"memoryId": candidate["id"]}, candidate_scope)
            return self.snapshot(candidate_scope=candidate_scope)

    def propose_preference(
        self,
        *,
        task: str,
        step: str,
        prompt: str,
        evidence: str,
        preference: str | None = None,
        source_quote: str | None = None,
        scope_reason: str = "当前任务与当前 checkpoint 在用户提出偏好时处于活动状态",
        candidate_scope: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            existing = self._candidate(candidate_scope)
            if existing:
                self._timeline.update_status(existing["id"], "deleted")
            value = preference or prompt
            candidate = self._timeline.remember(
                value,
                user_id=self.user_id,
                case_id=self._card_case_id(candidate_scope),
                scope=self.scope,
                kind="task-preference",
                tier="long",
                memory_key=f"task:{task}:step:{step}",
                status="pending",
                importance=1.0,
                metadata={
                    "trigger": {"task": str(task), "step": str(step)},
                    "preference": str(value),
                    "prompt": str(prompt),
                    "evidence": str(evidence),
                    "sourceQuote": str(source_quote or evidence),
                    "scopeReason": str(scope_reason),
                    "candidateScope": self._candidate_key(candidate_scope),
                },
                supersede=False,
            )
            if not candidate:
                raise RuntimeError("failed to create preference candidate")
            public = self._record(candidate)
            self._emit("memory.candidate.proposed", dict(public), candidate_scope)
            self._set_decision(
                self._decision("ask", "发现可能的长期偏好", "先询问是否保存，不从单次行为直接概括。"),
                candidate_scope,
            )
            return self.snapshot(candidate_scope=candidate_scope)

    def next_session(self) -> dict[str, Any]:
        with self._lock:
            self.session_number += 1
            self.session_events = []
            self._scoped_events = {}
            self._scoped_decisions = {}
            self.last_decision = self._decision(
                "silent", "新会话已开始", "等待步骤事件；长期记忆和待确认候选已从磁盘加载。",
            )
            return self.snapshot()

    def delete_memory(
        self,
        record_id: str,
        *,
        candidate_scope: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = next((item for item in self._records() if item["id"] == record_id), None)
            if record is None:
                raise ValueError("memory not found")
            self._timeline.delete(record_id)
            self._set_decision(
                self._decision("silent", "记忆已删除", "时间线记录已标记删除，后续不会再触发。"),
                candidate_scope,
            )
            self._emit("memory.deleted", {"memoryId": record_id}, candidate_scope)
            return self.snapshot(candidate_scope=candidate_scope)

    def retrieve(self, task: str, step: str, *, emit: bool = True) -> dict[str, Any]:
        with self._lock:
            candidates = []
            for item in self._records():
                task_match = item.get("trigger", {}).get("task") == task
                step_match = item.get("trigger", {}).get("step") == step
                selected = bool(task_match and step_match and item.get("status") == "confirmed")
                reasons = []
                if not task_match:
                    reasons.append("task-mismatch")
                if not step_match:
                    reasons.append("checkpoint-mismatch")
                if item.get("status") != "confirmed":
                    reasons.append("not-confirmed")
                candidates.append({
                    "memoryId": item.get("id"),
                    "selected": selected,
                    "score": 1.0 if selected else 0.0,
                    "match": {"task": task_match, "checkpoint": step_match},
                    "rejectionReasons": reasons,
                    "record": dict(item),
                })
            selected = next((item for item in candidates if item["selected"]), None)
            result = {
                "method": "timeline-exact-task-trigger-v1",
                "query": {"task": task, "checkpoint": step},
                "candidates": candidates,
                "selected": selected,
            }
            if emit:
                self._emit("memory.retrieval.completed", {
                    "method": result["method"],
                    "query": result["query"],
                    "candidateCount": len(candidates),
                    "selectedMemoryId": selected["memoryId"] if selected else None,
                })
            return result

    def matching_record(self, task: str, step: str) -> dict[str, Any] | None:
        selected = self.retrieve(task, step, emit=False)["selected"]
        return dict(selected["record"]) if selected else None

    def reset(self, clear_memory: bool = False) -> dict[str, Any]:
        with self._lock:
            if clear_memory:
                for card in self._timeline.cards(
                    user_id=self.user_id,
                    case_id=self.case_id if self.scope == "case" else None,
                    scope=self.scope,
                    kinds=("task-preference", "preference"),
                    statuses=("active", "pending"),
                ):
                    self._timeline.delete(card["id"])
            self.session_number = 1
            self.session_events = []
            self._scoped_events = {}
            self._scoped_decisions = {}
            self.last_decision = self._decision("silent", "等待事件", "尚无需要介入的证据。")
            return self.snapshot()

    def _records(self) -> list[dict[str, Any]]:
        cards = self._timeline.cards(
            user_id=self.user_id,
            case_id=self.case_id if self.scope == "case" else None,
            scope=self.scope,
            kinds=("task-preference", "preference"),
            statuses=("active",),
        )
        return [self._record(card) for card in cards]

    def _candidate(self, candidate_scope: str | None) -> dict[str, Any] | None:
        key = self._candidate_key(candidate_scope)
        cards = self._timeline.cards(
            user_id=self.user_id,
            scope=self.scope,
            kinds=("task-preference", "preference"),
            statuses=("pending",),
        )
        return next((
            self._record(card) for card in cards
            if card.get("metadata", {}).get("candidateScope") == key
        ), None)

    def _card_case_id(self, candidate_scope: str | None) -> str | None:
        return self.case_id or (str(candidate_scope) if candidate_scope else None)

    @staticmethod
    def _record(card: dict[str, Any]) -> dict[str, Any]:
        metadata = card.get("metadata", {})
        is_general = card.get("kind") == "preference"
        return {
            "id": card["id"],
            "trigger": {} if is_general else dict(metadata.get("trigger", {})),
            "preference": metadata.get("preference", card["text"]),
            "prompt": metadata.get("prompt", card["text"]),
            "evidence": metadata.get("evidence", ""),
            "sourceQuote": metadata.get("sourceQuote", ""),
            "scopeReason": metadata.get("scopeReason", ""),
            "status": "confirmed" if card["status"] == "active" else "pending",
            "confirmedAt": card["updatedAt"] if card["status"] == "active" else None,
            "kind": card.get("kind"),
            "scope": card.get("scope"),
            "memoryKey": card.get("memoryKey"),
        }

    def import_legacy(self, legacy_path: str | Path) -> int:
        legacy_path = Path(legacy_path)
        if not legacy_path.exists() or legacy_path == self.memory_path:
            return 0
        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(payload, dict):
            return 0
        imported = 0
        for record in payload.get("records", []):
            trigger = record.get("trigger", {})
            card = self._timeline.remember(
                str(record.get("preference") or record.get("prompt") or ""),
                user_id=self.user_id,
                case_id=self.case_id,
                scope=self.scope,
                kind="task-preference",
                tier="long",
                memory_key=f"task:{trigger.get('task')}:step:{trigger.get('step')}",
                status="active",
                importance=1.0,
                metadata={**dict(record), "candidateScope": "__default__"},
                supersede=False,
            )
            if card is None:
                continue
            imported += 1
        for key, candidate in payload.get("candidates", {}).items():
            trigger = candidate.get("trigger", {})
            card = self._timeline.remember(
                str(candidate.get("preference") or candidate.get("prompt") or ""),
                user_id=self.user_id,
                case_id=self.case_id or (None if key == "__default__" else key),
                scope=self.scope,
                kind="task-preference",
                tier="long",
                memory_key=f"task:{trigger.get('task')}:step:{trigger.get('step')}",
                status="pending",
                importance=1.0,
                metadata={**dict(candidate), "candidateScope": key},
                supersede=False,
            )
            imported += int(card is not None)
        return imported

    def _set_decision(self, decision: dict[str, Any], candidate_scope: str | None) -> None:
        if candidate_scope is None:
            self.last_decision = decision
        else:
            self._scoped_decisions[self._candidate_key(candidate_scope)] = decision

    def _append_event(self, event: str, detail: str, candidate_scope: str | None = None) -> None:
        target = self.session_events if candidate_scope is None else self._scoped_events.setdefault(
            self._candidate_key(candidate_scope), [],
        )
        target.append({"type": event, "detail": detail, "time": self._now()})

    def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        candidate_scope: str | None = None,
    ) -> None:
        self._append_event(event_type, str(payload)[:300], candidate_scope)
        if self._event_sink:
            self._event_sink(event_type, "preference-memory", payload)

    @staticmethod
    def _decision(action: str, message: str, reason: str, evidence_id: str | None = None) -> dict[str, Any]:
        return {"action": action, "message": message, "reason": reason, "evidenceId": evidence_id}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
