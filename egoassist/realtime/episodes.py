"""Realtime compatibility view over the shared timeline memory."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable

from egoassist.memory.timeline import (
    DEFAULT_RETRIEVABLE_KINDS, DEFAULT_RETRIEVAL_MIN_SCORE, TimelineMemory,
    looks_like_question,
)
from egoassist.memory.temporal import ClockContext


def retrieval_audit_from_events(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    retrieved = next((item for item in reversed(events) if item.get("type") == "episodic.memory.retrieved"), None)
    if not retrieved:
        return None
    injected = next((item for item in reversed(events) if item.get("type") == "episodic.memory.injected"
                     and item.get("parentEventIds") == retrieved.get("parentEventIds")), None)
    if not injected:
        return None
    payload = retrieved["payload"]
    injection = injected["payload"]
    evidence = {item["episodeId"]: item for item in injection.get("evidence", [])}
    candidates = [{**item, "record": evidence.get(item["episodeId"], {}),
                   "retrievalReasons": item.get("retrievalReasons", [])}
                  for item in payload.get("candidates", [])]
    return {**payload, "candidates": candidates, "selected": [item for item in candidates if item.get("selected")],
            "injection": injection, "auditEventId": retrieved["eventId"],
            "timestamp": retrieved.get("timestampWall"), "projection": "last-journalled-retrieval"}


class RealtimeEpisodeMemory:
    """Expose the former episodic API while storing typed timeline cards."""

    def __init__(
        self,
        path: str | Path,
        event_sink: Callable[..., Any] | None = None,
        capacity: int = 300,
        min_score: float = 0.05,
        *,
        timeline: TimelineMemory | None = None,
        owner_user_id: str | None = None,
        owner_case_id: str | None = None,
        scope: str | None = None,
    ) -> None:
        legacy_path = Path(path)
        timeline_path = legacy_path.with_name("timeline.sqlite3")
        self._timeline = timeline or TimelineMemory(
            timeline_path,
            short_term_capacity=max(4, int(capacity)),
            event_sink=event_sink,
        )
        self.path = self._timeline.path
        self.capacity = int(capacity)
        self.min_score = float(min_score)
        self._event_sink = event_sink
        self.owner_user_id = str(owner_user_id) if owner_user_id else None
        self.owner_case_id = str(owner_case_id) if owner_case_id else None
        self.scope = scope
        if self.scope not in {None, "case", "participant"}:
            raise ValueError("invalid episodic memory scope")
        self._last_retrieval_by_session: dict[str, dict[str, Any]] = {}
        # Preference compatibility layer (lazy-initialized on first use)
        self._pref_lock = threading.RLock()
        self._pref: "PreferenceMemory | None" = None  # type: ignore[name-defined]
        if timeline is None:
            self.import_legacy(legacy_path)

    def _identity(
        self,
        *,
        session_id: str | None,
        user_scope: str,
        allow_cross_session: bool = False,
    ) -> tuple[str, str | None, str]:
        user_id = self.owner_user_id or str(user_scope or "local-default")
        memory_scope = self.scope or ("participant" if allow_cross_session or session_id is None else "case")
        case_id = self.owner_case_id or session_id
        if memory_scope == "case" and not case_id:
            case_id = "local-default"
        return user_id, case_id, memory_scope

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
        memory_key: str | None = None,
        scope: str | None = None,
        tier: str = "short",
        status: str = "active",
        importance: float = 0.5,
        happened_at: str | None = None,
        recorded_at: str | None = None,
        effective_at: str | None = None,
        effective_end: str | None = None,
        timezone_name: str = "UTC",
        utc_offset_minutes: int = 0,
        recurrence: dict[str, Any] | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        value: str | None = None,
        candidate_scope: str | None = None,
        evidence_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        summary = str(summary).strip()
        if not summary:
            return None
        if source not in {"semantic-memory-card", "semantic-memory-recovery"} and kind in {"utterance", "query", "fact", "event", "state", "preference"} and looks_like_question(summary):
            return None
        user_id, case_id, configured_scope = self._identity(
            session_id=session_id, user_scope=user_scope,
            allow_cross_session=(scope or self.scope) == "participant",
        )
        memory_scope = scope or configured_scope
        card_kind = kind if kind in {
            "fact", "event", "state", "preference", "scene", "observation", "tool-effect", "legacy",
        } else "legacy"
        metadata = {
            "source": str(source),
            "skill": skill,
            "task": task,
            "evidenceEventId": evidence_event_id,
            "originalKind": str(kind),
            "candidateScope": str(candidate_scope or "__default__"),
            "evidenceContext": evidence_context or {},
        }
        card = self._timeline.remember(
            summary,
            user_id=user_id,
            case_id=case_id,
            scope=memory_scope,
            kind=card_kind,
            tier=tier,
            memory_key=memory_key,
            happened_at=happened_at,
            recorded_at=recorded_at,
            effective_at=effective_at,
            effective_end=effective_end,
            timezone_name=timezone_name,
            utc_offset_minutes=utc_offset_minutes,
            recurrence=recurrence,
            subject=subject,
            predicate=predicate,
            value=value,
            source_ids=[evidence_event_id] if evidence_event_id else [],
            status=status,
            importance=importance,
            metadata=metadata,
            supersede=bool(memory_key),
        )
        if not card:
            return None
        record = self._record(card)
        self._emit("episodic.memory.written", {"episode": record})
        return record

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
        clock: ClockContext | None = None,
        query_spec: dict[str, Any] | None = None,
        project: bool = True,
    ) -> dict[str, Any]:
        user_id, case_id, memory_scope = self._identity(
            session_id=session_id,
            user_scope=user_scope,
            allow_cross_session=allow_cross_session,
        )
        raw = self._timeline.retrieve(
            query,
            user_id=user_id,
            case_id=case_id,
            scope=memory_scope,
            limit=limit,
            kinds=DEFAULT_RETRIEVABLE_KINDS - {"task-preference"},
            clock=clock,
            query_spec=query_spec,
            min_score=self.min_score,
        )
        candidates = [self._candidate(item) for item in raw["candidates"]]
        selected_ids = {item["cardId"] for item in raw["selected"]}
        selected = [item for item in candidates if item["episodeId"] in selected_ids]
        result = {
            "method": raw["method"],
            "query": str(query),
            "referential": bool(referential),
            "scope": {
                "user": user_id,
                "session": case_id,
                "allowCrossSession": memory_scope == "participant",
                "memoryScope": memory_scope,
            },
            "routing": raw["routing"],
            "minimumScore": raw["minimumScore"],
            "timing": raw.get("timing", {}),
            "candidates": candidates,
            "selected": selected,
            "injection": {"status": "not-injected", "episodeIds": []},
            "claimBoundary": raw["claimBoundary"],
        }
        if project:
            key = session_id or "__default__"
            self._last_retrieval_by_session[key] = result
        if emit and session_id is None:
            self._emit("episodic.memory.retrieved", {
                "query": str(query),
                "candidateCount": len(candidates),
                "selectedEpisodeIds": [item["episodeId"] for item in selected],
            })
        return result

    def mark_injected(self, result: dict[str, Any], *, project: bool = True) -> dict[str, Any]:
        selected = result.get("selected", [])
        injection = {
            "status": "injected" if selected else "no-evidence",
            "queryContext": result.get("routing", {}).get("evidenceContext", {}),
            "temporalStatus": result.get("routing", {}).get("temporalStatus", "not-requested"),
            "query": str(result.get("query", "")),
            "episodeIds": [item["episodeId"] for item in selected],
            "cardIds": [item["episodeId"] for item in selected],
            "evidence": [{
                "episodeId": item["episodeId"],
                "cardId": item["episodeId"],
                "timestamp": item["record"]["timestamp"],
                "source": item["record"]["source"],
                "summary": item["record"]["summary"],
                "kind": item["record"]["kind"],
                "layer": item.get("layer"),
                "score": item["score"],
                "reasons": item.get("retrievalReasons", []),
                "recordedAt": item["record"].get("recordedAt"),
                "effectiveAt": item["record"].get("effectiveAt"),
                "effectiveEnd": item["record"].get("effectiveEnd"),
                "timezone": item["record"].get("timezone"),
                "sourceIds": item["record"].get("sourceIds", []),
                "evidenceContext": item["record"].get("evidenceContext", {}),
                "temporalSupport": item.get("temporalSupport", "not-requested"),
            } for item in sorted(selected, key=lambda value: value["record"]["timestamp"])],
        }
        result["injection"] = injection
        scope = result.get("scope", {})
        if project:
            key = str(scope.get("session") or "__default__")
            self._last_retrieval_by_session[key] = result
        if scope.get("session") is None:
            self._emit("episodic.memory.injected", injection)
        return injection

    def snapshot(
        self,
        *,
        session_id: str | None = None,
        user_scope: str = "local-default",
        allow_cross_session: bool = False,
        allow_participant_overlay: bool = False,
    ) -> dict[str, Any]:
        user_id, case_id, memory_scope = self._identity(
            session_id=session_id,
            user_scope=user_scope,
            allow_cross_session=allow_cross_session,
        )
        timeline_state = self._timeline.snapshot(
            user_id=user_id,
            case_id=case_id,
            scope=memory_scope,
        )
        retrieval = self._last_retrieval_by_session.get(session_id or "__default__")
        visible_scopes = {memory_scope}
        if memory_scope == "case" and allow_participant_overlay:
            visible_scopes.add("case+participant")
        if retrieval is None or retrieval.get("scope", {}).get("memoryScope") not in visible_scopes:
            retrieval = self.retrieve(
                "", emit=False, session_id=session_id, user_scope=user_scope,
                allow_cross_session=allow_cross_session,
            )
        return {
            "path": str(self.path),
            "count": timeline_state["count"],
            "layers": timeline_state["layers"],
            "recent": [self._record(item) for item in timeline_state["recent"]],
            "retrieval": retrieval,
            "scope": memory_scope,
            "retainsRawMedia": False,
            "evidenceNetwork": timeline_state.get("evidenceNetwork", {}),
        }

    def consolidate(
        self,
        *,
        session_id: str | None = None,
        user_scope: str = "local-default",
        allow_cross_session: bool = False,
    ) -> dict[str, Any]:
        user_id, case_id, memory_scope = self._identity(
            session_id=session_id,
            user_scope=user_scope,
            allow_cross_session=allow_cross_session,
        )
        return self._timeline.promote_to_long(
            user_id=user_id,
            case_id=case_id,
            scope=memory_scope,
        )

    def import_legacy(self, legacy_path: str | Path) -> int:
        legacy_path = Path(legacy_path)
        if not legacy_path.exists() or legacy_path == self.path:
            return 0
        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        imported = 0
        for record in payload.get("records", []) if isinstance(payload, dict) else []:
            summary = str(record.get("summary", ""))
            if not summary or looks_like_question(summary):
                continue
            session_id = record.get("sessionId")
            card = self.remember(
                summary,
                kind=str(record.get("kind", "legacy")),
                source=str(record.get("source", "legacy-import")),
                skill=record.get("skill"),
                task=record.get("task"),
                evidence_event_id=record.get("evidenceEventId"),
                session_id=session_id,
                user_scope=str(record.get("userScope", "local-default")),
                scope="case" if session_id else "participant",
                happened_at=record.get("timestamp"),
            )
            imported += int(card is not None)
        return imported

    @staticmethod
    def _record(card: dict[str, Any]) -> dict[str, Any]:
        metadata = card.get("metadata", {})
        return {
            "id": card["id"],
            "timestamp": card["happenedAt"],
            "kind": metadata.get("originalKind") or card["kind"],
            "summary": card["text"],
            "source": metadata.get("source", "timeline-memory"),
            "skill": metadata.get("skill"),
            "task": metadata.get("task"),
            "evidenceEventId": metadata.get("evidenceEventId"),
            "sessionId": card.get("caseId"),
            "userScope": card.get("userId"),
            "scope": card.get("scope"),
            "tier": card.get("tier"),
            "memoryKey": card.get("memoryKey"),
            "recordedAt": card.get("recordedAt"),
            "effectiveAt": card.get("effectiveAt"),
            "effectiveEnd": card.get("effectiveEnd"),
            "timezone": card.get("timezone"),
            "recurrence": card.get("recurrence"),
            "subject": card.get("subject"),
            "predicate": card.get("predicate"),
            "value": card.get("value"),
            "status": card.get("status"),
            "sourceIds": card.get("sourceIds", []),
            "evidenceContext": metadata.get("evidenceContext", {}),
        }

    @classmethod
    def _candidate(cls, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "episodeId": item["cardId"],
            "score": item["score"],
            "selected": item["selected"],
            "rejectionReasons": list(item.get("rejectionReasons", [])),
            "retrievalReasons": list(item.get("reasons", [])),
            "temporalSupport": item.get("temporalSupport", "not-requested"),
            "scopeMatch": True,
            "sessionMatch": True,
            "layer": item.get("layer"),
            "record": cls._record(item["card"]),
        }

    # ------------------------------------------------------------------
    # Preference delegation — unified interface over the same timeline DB
    # ------------------------------------------------------------------

    def _get_pref(self) -> "PreferenceMemory":  # type: ignore[name-defined]
        """Lazy-initialize the preference layer sharing the same TimelineMemory.

        Confirmed preferences are *participant*-durable, independent of this
        episode memory's own ``self.scope``. Before the timeline unification the
        memory policy carried two independent scopes: every profile that enabled
        preferences used ``preference.scope == "participant"`` while
        ``episodic.scope`` was ``"case"``. Unification collapsed both onto a
        single ``timeline.scope`` (case for most profiles), which silently
        demoted preferences to case scope — so a preference confirmed on the
        participant object (e.g. the sessionless /api/voice + /api/candidate
        write path) became invisible to a case-scoped reader at WS-connect, and
        ``impact.status`` regressed from ``reused`` to ``none``.

        Pinning the preference sub-store to ``participant`` restores the
        pre-unification contract and mirrors the participant-durable read in
        ``state()``'s preference snapshot. ``timeline.scope`` continues to govern
        the episodic timeline only. ``case_id`` is irrelevant for participant
        scope (``_records`` filters ``case_id=None`` there), so it is dropped.
        """
        with self._pref_lock:
            if self._pref is None:
                from egoassist.realtime.engine import PreferenceMemory
                self._pref = PreferenceMemory(
                    self._timeline.path,
                    event_sink=self._event_sink,
                    timeline=self._timeline,
                    user_id=self.owner_user_id or "local-default",
                    scope="participant",
                )
            return self._pref

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
        """Propose a task-preference candidate (pending, awaiting confirmation)."""
        return self._get_pref().propose_preference(
            task=task,
            step=step,
            prompt=prompt,
            evidence=evidence,
            preference=preference,
            source_quote=source_quote,
            scope_reason=scope_reason,
            candidate_scope=candidate_scope,
        )

    def resolve_candidate(
        self,
        accepted: bool,
        *,
        candidate_scope: str | None = None,
    ) -> dict[str, Any]:
        """Accept or reject the pending preference candidate."""
        return self._get_pref().resolve_candidate(accepted, candidate_scope=candidate_scope)

    def retrieve_preference(
        self,
        task: str,
        step: str,
        *,
        emit: bool = True,
    ) -> dict[str, Any]:
        """Exact task+step trigger lookup for confirmed task-preference cards."""
        return self._get_pref().retrieve(task, step, emit=emit)

    def snapshot_preference(
        self,
        *,
        candidate_scope: str | None = None,
    ) -> dict[str, Any]:
        """Preference-style snapshot: {session, memoryPath, records, candidate, decision, events}."""
        return self._get_pref().snapshot(candidate_scope=candidate_scope)

    def delete_preference(
        self,
        record_id: str,
        *,
        candidate_scope: str | None = None,
    ) -> dict[str, Any]:
        """Delete a confirmed or pending preference card by ID."""
        return self._get_pref().delete_memory(record_id, candidate_scope=candidate_scope)

    def import_preference_legacy(self, legacy_path: str | "Path") -> int:
        """Migrate old preferences.json records into the shared timeline DB."""
        return self._get_pref().import_legacy(legacy_path)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._event_sink:
            self._event_sink(event_type, "timeline-memory", payload)
