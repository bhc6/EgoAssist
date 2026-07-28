"""Append-only observable event ledger for reproducible realtime experiments."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


class EventLedger:
    """Keep causal, externally observable events without exposing hidden reasoning."""

    def __init__(self, session_id: str | None = None, capacity: int = 2000) -> None:
        self.session_id = session_id or uuid4().hex[:12]
        self.capacity = capacity
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []

    def append(
        self,
        event_type: str,
        source: str,
        payload: dict[str, Any] | None = None,
        *,
        parent_event_ids: list[str] | tuple[str, ...] = (),
        model: str | None = None,
        skill: str | None = None,
        task: str | None = None,
        response_id: str | None = None,
        latency_ms: float | None = None,
    ) -> dict[str, Any]:
        event = {
            "eventId": uuid4().hex[:12],
            "parentEventIds": list(parent_event_ids),
            "sessionId": self.session_id,
            "timestampWall": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "timestampMonotonic": round(time.monotonic(), 6),
            "type": event_type,
            "source": source,
            "payload": payload or {},
            "model": model,
            "skill": skill,
            "task": task,
            "responseId": response_id,
            "latencyMs": round(latency_ms, 2) if latency_ms is not None else None,
        }
        with self._lock:
            self._events.append(event)
            if len(self._events) > self.capacity:
                del self._events[: len(self._events) - self.capacity]
        return dict(event)

    def snapshot(self, limit: int = 160) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._events[-limit:]]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

