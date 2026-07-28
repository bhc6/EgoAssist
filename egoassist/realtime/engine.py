"""Minimal persistent memory loop for future-oriented assistance."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


class PreferenceMemory:
    """Store confirmed task-step preferences for any Skill and task.

    Events can come from the research console or any perception adapter posting JSON.
    The engine deliberately does not perform vision inference itself.
    """

    def __init__(
        self,
        memory_path: str | Path = ".egoassist/preferences.json",
        event_sink: Callable[[str, str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.memory_path = Path(memory_path)
        self._lock = threading.RLock()
        self.session_number = 1
        self.session_events: list[dict[str, Any]] = []
        self.candidate: dict[str, Any] | None = None
        self.last_decision = self._decision("silent", "等待事件", "尚无需要介入的证据。")
        self._records = self._load()
        self._event_sink = event_sink

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session": self.session_number,
                "memoryPath": str(self.memory_path),
                "records": list(self._records),
                "candidate": self.candidate,
                "decision": self.last_decision,
                "events": list(reversed(self.session_events[-6:])),
            }

    def resolve_candidate(self, accepted: bool) -> dict[str, Any]:
        with self._lock:
            if self.candidate is None:
                raise ValueError("no pending memory candidate")
            if accepted:
                record = {**self.candidate, "status": "confirmed", "confirmedAt": self._now()}
                self._records = [item for item in self._records if item["trigger"] != record["trigger"]]
                self._records.append(record)
                self._save()
                self.last_decision = self._decision("saved", "已保存", "这条记忆将在后续相同步骤中复用，可随时删除。", evidence_id=record["id"])
                self._emit("memory.confirmed", {"memoryId": record["id"], "trigger": record["trigger"], "preference": record["preference"]})
            else:
                self.last_decision = self._decision("silent", "未保存", "候选偏好已丢弃，不影响未来会话。")
                self._emit("memory.rejected", {"memoryId": self.candidate["id"]})
            self.candidate = None
            return self.snapshot()

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
    ) -> dict[str, Any]:
        """Create an explicit, user-confirmable preference for any task."""
        with self._lock:
            self.candidate = {
                "id": uuid4().hex[:8],
                "trigger": {"task": task, "step": step},
                "preference": preference or prompt,
                "prompt": prompt,
                "evidence": evidence,
                "sourceQuote": source_quote or evidence,
                "scopeReason": scope_reason,
                "status": "pending",
            }
            self._emit("memory.candidate.proposed", dict(self.candidate))
            self.last_decision = self._decision(
                "ask",
                "发现可能的长期偏好",
                "先询问是否保存，不从单次行为直接概括。",
            )
            return self.snapshot()

    def next_session(self) -> dict[str, Any]:
        with self._lock:
            self.session_number += 1
            self.session_events = []
            self.candidate = None
            self.last_decision = self._decision("silent", "新会话已开始", "等待步骤事件；长期记忆已从磁盘加载。")
            return self.snapshot()

    def delete_memory(self, record_id: str) -> dict[str, Any]:
        with self._lock:
            before = len(self._records)
            self._records = [item for item in self._records if item["id"] != record_id]
            if len(self._records) == before:
                raise ValueError("memory not found")
            self._save()
            self.last_decision = self._decision("silent", "记忆已删除", "磁盘记录已同步更新，后续不会再触发。")
            self._emit("memory.deleted", {"memoryId": record_id})
            return self.snapshot()

    def retrieve(self, task: str, step: str, *, emit: bool = True) -> dict[str, Any]:
        """Return an auditable exact-trigger lookup, including rejected records."""
        with self._lock:
            candidates = []
            for item in reversed(self._records):
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
                    "memoryId": item.get("id"), "selected": selected,
                    "score": 1.0 if selected else 0.0,
                    "match": {"task": task_match, "checkpoint": step_match},
                    "rejectionReasons": reasons,
                    "record": dict(item),
                })
            selected = next((item for item in candidates if item["selected"]), None)
            result = {
                "method": "exact-trigger-lookup-v1",
                "query": {"task": task, "checkpoint": step},
                "candidates": candidates,
                "selected": selected,
            }
            if emit:
                self._emit("memory.retrieval.completed", {
                    "method": result["method"], "query": result["query"],
                    "candidateCount": len(candidates),
                    "selectedMemoryId": selected["memoryId"] if selected else None,
                })
            return result

    def reset(self, clear_memory: bool = False) -> dict[str, Any]:
        with self._lock:
            if clear_memory:
                self._records = []
                self._save()
            self.session_number = 1
            self.session_events = []
            self.candidate = None
            self.last_decision = self._decision("silent", "等待事件", "尚无需要介入的证据。")
            return self.snapshot()

    def _matching_record(self, task: str, step: str) -> dict[str, Any] | None:
        return next((item for item in reversed(self._records) if item["status"] == "confirmed" and item["trigger"] == {"task": task, "step": step}), None)

    def matching_record(self, task: str, step: str) -> dict[str, Any] | None:
        """Return a copy of a confirmed record for runtime personalization."""
        with self._lock:
            record = self._matching_record(task, step)
            return dict(record) if record else None

    def _append_event(self, event: str, detail: str) -> None:
        self.session_events.append({"type": event, "detail": detail, "time": self._now()})

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        self._append_event(event_type, str(payload)[:300])
        if self._event_sink:
            self._event_sink(event_type, "preference-memory", payload)

    def _load(self) -> list[dict[str, Any]]:
        if not self.memory_path.exists():
            return []
        try:
            payload = json.loads(self.memory_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid memory store: {self.memory_path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError(f"invalid memory store schema: {self.memory_path}")
        return payload["records"]

    def _save(self) -> None:
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.memory_path.with_suffix(self.memory_path.suffix + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "records": self._records}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.memory_path)

    @staticmethod
    def _decision(action: str, message: str, reason: str, evidence_id: str | None = None) -> dict[str, Any]:
        return {"action": action, "message": message, "reason": reason, "evidenceId": evidence_id}

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
