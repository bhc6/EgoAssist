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
        min_score: float = 0.05,
    ) -> None:
        self.path = Path(path)
        self.capacity = capacity
        if not 0 < min_score <= 1:
            raise ValueError("min_score must be in (0, 1]")
        self.min_score = float(min_score)
        self._event_sink = event_sink
        self._lock = threading.RLock()
        self._records = self._load()
        self._last_retrieval_by_session: dict[str, dict[str, Any]] = {}
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
        session_id: str | None = None,
        user_scope: str = "local-default",
    ) -> dict[str, Any] | None:
        summary = " ".join(str(summary).split()).strip()[:500]
        if not summary:
            return None
        with self._lock:
            previous = self._records[-1] if self._records else None
            if (
                previous
                and previous["kind"] == kind
                and previous["summary"] == summary
                and previous.get("sessionId") == session_id
                and previous.get("userScope", "local-default") == user_scope
            ):
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
                "sessionId": session_id,
                "userScope": user_scope,
            }
            self._records.append(record)
            if len(self._records) > self.capacity:
                del self._records[:len(self._records) - self.capacity]
            self._save()
            if session_id is None:
                self._emit("episodic.memory.written", {"episode": record})
            return dict(record)

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        *,
        emit: bool = True,
        session_id: str | None = None,
        user_scope: str = "local-default",
        allow_cross_session: bool = False,
        referential: bool = False,
    ) -> dict[str, Any]:
        query_terms = _terms(query)
        referential = bool(referential)
        with self._lock:
            candidates = []
            chronological = [dict(record) for record in self._records]
            for recency, record in enumerate(reversed(chronological)):
                index = len(chronological) - recency - 1
                record_terms = _terms(record["summary"])
                overlap = len(query_terms & record_terms)
                denominator = max(1, len(query_terms | record_terms))
                scope_match = record.get("userScope", "local-default") == user_scope
                session_match = (
                    record.get("sessionId") is None
                    if session_id is None
                    else record.get("sessionId") == session_id
                )
                eligible = scope_match and (allow_cross_session or session_match)
                score = overlap / denominator if query_terms and eligible else 0.0
                rejection_reasons = []
                if not scope_match:
                    rejection_reasons.append("user-scope-mismatch")
                if not allow_cross_session and not session_match:
                    rejection_reasons.append("session-mismatch")
                if not overlap:
                    rejection_reasons.append("no-term-overlap")
                candidates.append({
                    "episodeId": record["id"],
                    "score": round(score, 4),
                    "selected": False,
                    "rejectionReasons": rejection_reasons,
                    "retrievalReasons": ["term-overlap"] if overlap and eligible else [],
                    "scopeMatch": scope_match,
                    "sessionMatch": session_match,
                    "record": dict(record),
                    "recencyRank": recency,
                    "chronologicalIndex": index,
                })

            # A referential question often names an earlier instruction but not
            # its value. Expand one utterance around lexical anchors so a pair
            # such as "remember this" -> "value" stays retrievable without a
            # domain-specific schema or opaque embedding score.
            by_index = {item["chronologicalIndex"]: item for item in candidates}
            anchors = [item for item in candidates if item["score"] > 0]
            if referential:
                for anchor in anchors:
                    for neighbor_index in (anchor["chronologicalIndex"] - 1, anchor["chronologicalIndex"] + 1):
                        neighbor = by_index.get(neighbor_index)
                        if not neighbor or neighbor["record"].get("kind") != "utterance":
                            continue
                        if not neighbor["scopeMatch"] or (not allow_cross_session and not neighbor["sessionMatch"]):
                            continue
                        temporal_score = max(0.05, min(0.95, anchor["score"] * 0.85))
                        if temporal_score > neighbor["score"]:
                            neighbor["score"] = temporal_score
                        neighbor["retrievalReasons"].append(
                            f"temporal-neighbor-of:{anchor['episodeId']}"
                        )
                        neighbor["rejectionReasons"] = [
                            reason for reason in neighbor["rejectionReasons"] if reason != "no-term-overlap"
                        ]

            for item in candidates:
                item["score"] = round(float(item["score"]), 4)
            candidates.sort(key=lambda item: (-item["score"], item["recencyRank"]))
            selected = [item for item in candidates if item["score"] >= self.min_score][:limit]
            selected_ids = {item["episodeId"] for item in selected}
            for item in candidates:
                item["selected"] = item["episodeId"] in selected_ids
                if item["score"] > 0 and not item["selected"]:
                    item["rejectionReasons"].append(
                        "below-score-threshold" if item["score"] < self.min_score else "outside-top-k"
                    )
            result = {
                "method": "auditable-temporal-retrieval-v2",
                "query": query,
                "referential": referential,
                "scope": {
                    "user": user_scope,
                    "session": session_id,
                    "allowCrossSession": allow_cross_session,
                },
                "minimumScore": self.min_score,
                "candidates": candidates[:20],
                "selected": selected,
                "injection": {"status": "not-injected", "episodeIds": []},
                "claimBoundary": "Text summaries and timestamp adjacency only; no raw frame retention or opaque embedding semantics.",
            }
            self._last_retrieval = result
            self._last_retrieval_by_session[session_id or "__default__"] = result
            if emit and session_id is None:
                self._emit("episodic.memory.retrieved", {
                    "query": query,
                    "candidateCount": len(candidates),
                    "selectedEpisodeIds": [item["episodeId"] for item in selected],
                })
            return result

    def mark_injected(self, result: dict[str, Any]) -> dict[str, Any]:
        selected = result.get("selected", [])
        injection = {
            "status": "injected" if selected else "no-evidence",
            "query": str(result.get("query", "")),
            "episodeIds": [item["episodeId"] for item in selected],
            "evidence": [
                {
                    "episodeId": item["episodeId"],
                    "timestamp": item["record"]["timestamp"],
                    "source": item["record"]["source"],
                    "summary": item["record"]["summary"],
                    "score": item["score"],
                    "reasons": item.get("retrievalReasons", []),
                }
                for item in sorted(selected, key=lambda value: value["record"]["timestamp"])
            ],
        }
        with self._lock:
            result["injection"] = injection
            self._last_retrieval = result
            scope = result.get("scope", {})
            self._last_retrieval_by_session[str(scope.get("session") or "__default__")] = result
        if result.get("scope", {}).get("session") is None:
            self._emit("episodic.memory.injected", injection)
        return injection

    def snapshot(
        self,
        *,
        session_id: str | None = None,
        user_scope: str = "local-default",
        allow_cross_session: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            visible = [
                item for item in self._records
                if item.get("userScope", "local-default") == user_scope
                and (
                    allow_cross_session
                    or (item.get("sessionId") is None if session_id is None
                        else item.get("sessionId") == session_id)
                )
            ]
            retrieval = self._last_retrieval_by_session.get(session_id or "__default__")
            if (
                retrieval is None
                or bool(retrieval.get("scope", {}).get("allowCrossSession")) != allow_cross_session
            ):
                retrieval = self.retrieve(
                    "", emit=False, session_id=session_id, user_scope=user_scope,
                    allow_cross_session=allow_cross_session,
                )
            return {
                "path": str(self.path),
                "count": len(visible),
                "recent": [dict(item) for item in reversed(visible[-12:])],
                "retrieval": retrieval,
                "scope": "participant" if allow_cross_session else "case",
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
