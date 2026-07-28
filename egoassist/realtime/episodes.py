"""Small persistent adapter from realtime summaries to auditable episodic recall."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


def _terms(text: str) -> set[str]:
    normalized = text.casefold()
    latin = set(re.findall(r"[a-z0-9]{2,}", normalized))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    return latin | {cjk[index:index + 2] for index in range(max(0, len(cjk) - 1))}


class RealtimeEpisodeMemory:
    """Persist compact scene/dialogue episodes without retaining raw media."""

    def __init__(
        self,
        path: str | Path,
        event_sink: Callable[..., Any] | None = None,
        capacity: int = 300,
    ) -> None:
        self.path = Path(path)
        self.capacity = capacity
        self._event_sink = event_sink
        self._lock = threading.RLock()
        self._records = self._load()
        self._last_retrieval = self.retrieve("", emit=False)

    def remember(
        self,
        summary: str,
        *,
        kind: str,
        source: str,
        skill: str | None,
        task: str | None,
        evidence_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        summary = " ".join(str(summary).split()).strip()[:500]
        if not summary:
            return None
        with self._lock:
            previous = self._records[-1] if self._records else None
            if previous and previous["kind"] == kind and previous["summary"] == summary:
                return dict(previous)
            record = {
                "id": uuid4().hex[:10],
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "kind": kind,
                "summary": summary,
                "source": source,
                "skill": skill,
                "task": task,
                "evidenceEventId": evidence_event_id,
            }
            self._records.append(record)
            if len(self._records) > self.capacity:
                del self._records[:len(self._records) - self.capacity]
            self._save()
            self._emit("episodic.memory.written", {"episode": record})
            return dict(record)

    def retrieve(self, query: str, limit: int = 5, *, emit: bool = True) -> dict[str, Any]:
        query_terms = _terms(query)
        with self._lock:
            candidates = []
            for recency, record in enumerate(reversed(self._records)):
                record_terms = _terms(record["summary"])
                overlap = len(query_terms & record_terms)
                denominator = max(1, len(query_terms | record_terms))
                score = overlap / denominator if query_terms else 0.0
                candidates.append({
                    "episodeId": record["id"],
                    "score": round(score, 4),
                    "selected": False,
                    "rejectionReasons": [] if overlap else ["no-term-overlap"],
                    "record": dict(record),
                    "recencyRank": recency,
                })
            candidates.sort(key=lambda item: (-item["score"], item["recencyRank"]))
            selected = [item for item in candidates if item["score"] > 0][:limit]
            selected_ids = {item["episodeId"] for item in selected}
            for item in candidates:
                item["selected"] = item["episodeId"] in selected_ids
                if item["score"] > 0 and not item["selected"]:
                    item["rejectionReasons"].append("outside-top-k")
            result = {
                "method": "auditable-term-overlap-v1",
                "query": query,
                "candidates": candidates[:20],
                "selected": selected,
                "claimBoundary": "Text summaries only; no raw frame retention or embedding semantics.",
            }
            self._last_retrieval = result
            if emit:
                self._emit("episodic.memory.retrieved", {
                    "query": query,
                    "candidateCount": len(candidates),
                    "selectedEpisodeIds": [item["episodeId"] for item in selected],
                })
            return result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "path": str(self.path),
                "count": len(self._records),
                "recent": [dict(item) for item in reversed(self._records[-12:])],
                "retrieval": self._last_retrieval,
                "retainsRawMedia": False,
            }

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid episodic memory store: {self.path}") from exc
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise ValueError(f"invalid episodic memory schema: {self.path}")
        return records[-self.capacity:]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "records": self._records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_sink:
            self._event_sink(event_type, "realtime-episodic-memory", payload)
