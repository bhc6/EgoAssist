from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from egoassist.memory import resolve_time_semantics
from egoassist.memory.evidence import ground_context
from egoassist.realtime import memory_draft_candidate


@dataclass
class MemoryCommitService:
    append_memory_audit: Any
    case_owners: Any
    episodic_for_user: Any
    memory_recovery: Any
    timeline: Any

    def commit(
        self,
        *,
        active_episodic,
        active_runtime,
        clock_context,
        episodic_settings,
        harness,
        preference_enabled,
        semantic_decision,
        semantic_method,
        transcript,
        transcript_event,
        turn_id,
        user_scope,
    ):
        memory_write_started = time.perf_counter()
        timeline_write = None
        memory_kind = semantic_decision.get("memoryKind")
        memory_consent = semantic_decision.get("memoryConsent")
        memory_case_id = harness.session_id if harness else "local-default"
        pending_draft = self.timeline.pending_draft(
            user_id=user_scope, case_id=memory_case_id
        )
        raw_draft_decision = semantic_decision.get("memoryDraft")
        memory_atoms = list(semantic_decision.get("memoryAtoms") or [])
        recovery_pending = semantic_decision.get("status") in {
            "unavailable",
            "invalid",
        } or any(
            (
                error.get("component") == "memoryAtoms"
                for error in semantic_decision.get("componentErrors") or []
            )
        )
        if recovery_pending and harness:
            self.memory_recovery.submit(
                harness.journal,
                user_id=user_scope,
                turn_id=turn_id or transcript_event["eventId"],
                text=transcript,
                source_id=transcript_event["eventId"],
                clock=clock_context.prompt_payload(),
                draft=pending_draft,
                scene=semantic_decision.get("sourceScene"),
                memory_candidates=semantic_decision.get("sourceMemoryCandidates"),
            )
        if recovery_pending:
            memory_atoms = []
            raw_draft_decision = None
        if not recovery_pending and not memory_atoms and memory_kind in {"fact", "event", "state", "preference"}:
            memory_atoms = [
                {
                    "kind": memory_kind,
                    "text": semantic_decision.get("memoryText") or transcript,
                    "subject": None,
                    "predicate": None,
                    "value": semantic_decision.get("memoryText") or transcript,
                    "scope": semantic_decision.get("memoryScope") or "case",
                    "consent": semantic_decision.get("memoryConsent") or "observed",
                    "legacyMemoryKey": semantic_decision.get("memoryKey"),
                }
            ]
        draft_decision = raw_draft_decision
        if memory_kind == "capture" and (not draft_decision):
            draft_decision = {
                "mode": "start",
                "subject": None,
                "predicate": "memory.value",
                "language": None,
                "missingFields": ["subject", "value"],
            }
        draft_decision, draft_rejection = memory_draft_candidate(
            transcript, draft_decision, pending_draft
        )
        memory_requested = bool(draft_decision or memory_consent == "explicit" or semantic_decision.get("memoryRequestRejected"))
        ignored_draft = None
        if memory_atoms and raw_draft_decision:
            ignored_draft = {
                "reason": "memory-atoms-precedence",
                "candidate": raw_draft_decision,
            }
        if memory_atoms:
            written_cards = []
            skipped_reasons = []
            consumed_draft = False
            for atom in memory_atoms:
                atom_kind = atom.get("kind")
                atom_consent = atom.get("consent") or "observed"
                requested_scope = atom.get("scope") or "case"
                compatible_draft = bool(
                    pending_draft
                    and (
                        self.timeline.draft_accepts(
                            pending_draft,
                            subject=atom.get("subject"),
                            predicate=atom.get("predicate"),
                        )
                        or (
                            pending_draft.get("predicate") == "memory.value"
                            and (
                                semantic_method == "explicit-research-api-labels-v1"
                                or (
                                    atom.get("scope") == "participant"
                                    and atom.get("consent") == "explicit"
                                )
                            )
                        )
                    )
                )
                if compatible_draft:
                    requested_scope = "participant"
                    atom_consent = "explicit"
                write_enabled = (
                    preference_enabled
                    if requested_scope == "participant"
                    else episodic_settings["episodic_enabled"]
                )
                if not write_enabled:
                    skipped_reasons.append(f"{requested_scope}-memory-disabled")
                    continue
                target_episodic = (
                    self.episodic_for_user(user_scope)
                    if requested_scope == "participant"
                    and user_scope != "local-default"
                    else active_episodic
                )
                temporal = resolve_time_semantics(
                    atom.get("text") or transcript, clock_context, kind=atom_kind
                )
                evidence_context = ground_context(
                    atom.get("context"), transcript, clock_context
                )
                evidence_context["sourceText"] = transcript
                evidence_context["sourceSessionId"] = memory_case_id
                evidence_context["speaker"] = (
                    semantic_decision.get("sourceScene") or {}
                ).get("speaker")
                episode = target_episodic.remember(
                    atom.get("text") or transcript,
                    kind=atom_kind,
                    source="semantic-memory-card",
                    skill=active_runtime.skill.name if active_runtime.skill else None,
                    task=active_runtime.task.spec.id if active_runtime.task else None,
                    evidence_event_id=transcript_event["eventId"],
                    session_id=memory_case_id,
                    user_scope=user_scope,
                    memory_key=atom.get("legacyMemoryKey"),
                    scope=requested_scope,
                    tier="long"
                    if requested_scope == "participant" and atom_consent == "explicit"
                    else "short",
                    status="pending" if atom_consent == "inferred" else "active",
                    importance=1.0 if atom_consent == "explicit" else 0.7,
                    recorded_at=temporal["recordedAt"],
                    effective_at=temporal["effectiveAt"],
                    effective_end=temporal["effectiveEnd"],
                    timezone_name=temporal["timezone"],
                    utc_offset_minutes=clock_context.utc_offset_minutes,
                    recurrence=temporal["recurrence"],
                    subject=atom.get("subject"),
                    predicate=atom.get("predicate"),
                    value=atom.get("value"),
                    candidate_scope=memory_case_id,
                    evidence_context=evidence_context,
                    replaces_card_id=atom.get("replaces"),
                )
                if episode:
                    if episode["id"] not in {card["id"] for card in written_cards}:
                        written_cards.append(episode)
                    consumed_draft = consumed_draft or compatible_draft
                    _enc_owner = self.case_owners.get(memory_case_id)
                    if _enc_owner and _enc_owner.memory_bus:
                        _enc_owner.memory_bus.record(
                            "encode",
                            tier="long"
                            if requested_scope == "participant"
                            and atom_consent == "explicit"
                            else "short",
                            kind=atom_kind,
                            scope=requested_scope,
                            text_preview=str(atom.get("text") or transcript)[:120],
                            card_id=episode.get("id"),
                            consent=atom_consent,
                        )
                else:
                    skipped_reasons.append("empty-or-question-like-memory-text")
            if consumed_draft and pending_draft:
                self.timeline.finish_draft(pending_draft["id"], status="consumed")
            memory_requested = memory_requested or consumed_draft
            committed_ids = [
                card["id"]
                for card in written_cards
                if card.get("id") and card.get("status") == "active"
            ]
            if written_cards:
                scopes = {card.get("scope") for card in written_cards}
                timeline_write = {
                    "action": "written",
                    "card": written_cards[0],
                    "cards": written_cards,
                    "consent": "explicit" if consumed_draft else None,
                    "storageScope": next(iter(scopes)) if len(scopes) == 1 else "mixed",
                    "committedCardIds": committed_ids,
                    "ignoredDraft": ignored_draft,
                }
            else:
                timeline_write = {
                    "action": "not-written",
                    "status": "rejected",
                    "card": None,
                    "cards": [],
                    "reason": skipped_reasons[0]
                    if skipped_reasons
                    else "no-valid-memory-atoms",
                    "committedCardIds": [],
                    "ignoredDraft": ignored_draft,
                }
        elif draft_decision and preference_enabled:
            if (
                draft_decision.get("mode") == "continue"
                and pending_draft
                and self.timeline.draft_accepts(
                    pending_draft,
                    subject=draft_decision.get("subject"),
                    predicate=draft_decision.get("predicate"),
                )
            ):
                draft = self.timeline.update_draft(
                    pending_draft["id"],
                    subject=draft_decision.get("subject"),
                    predicate=draft_decision.get("predicate"),
                    missing_fields=draft_decision.get("missingFields"),
                    source_ids=[transcript_event["eventId"]],
                )
            elif draft_decision.get("mode") == "start" or not pending_draft:
                draft = self.timeline.begin_draft(
                    user_id=user_scope,
                    case_id=memory_case_id,
                    subject=draft_decision.get("subject"),
                    predicate=draft_decision.get("predicate") or "memory.value",
                    missing_fields=draft_decision.get("missingFields") or ("value",),
                    language=draft_decision.get("language"),
                    source_ids=[transcript_event["eventId"]],
                )
            else:
                draft = pending_draft
            timeline_write = {
                "action": "capture",
                "card": {**draft, "kind": "capture", "tier": "short", "scope": "case"},
                "draft": draft,
                "committedCardIds": [],
            }
        elif draft_decision:
            timeline_write = {
                "action": "not-written",
                "status": "disabled",
                "reason": "participant-memory-disabled",
                "card": None,
                "cards": [],
                "committedCardIds": [],
            }
        elif raw_draft_decision:
            timeline_write = {
                "action": "not-written",
                "status": "rejected",
                "card": None,
                "cards": [],
                "reason": draft_rejection or "draft-candidate-rejected",
                "committedCardIds": [],
            }
        elif memory_requested:
            timeline_write = {
                "action": "not-written",
                "status": "rejected",
                "card": None,
                "cards": [],
                "reason": "explicit-memory-request-had-no-committable-value",
                "committedCardIds": [],
            }
        if recovery_pending:
            timeline_write = {
                "action": "not-written",
                "status": "queued",
                "card": None,
                "cards": [],
                "reason": "raw-input-retained-memory-recovery-queued",
                "committedCardIds": [],
            }
        if harness and timeline_write:
            memory_write_ms = round(
                (time.perf_counter() - memory_write_started) * 1000, 3
            )
            card = timeline_write.get("card", {})
            cards = timeline_write.get("cards") or ([card] if card else [])
            event_type = {
                "capture": "timeline.memory.capture.started",
                "written": "timeline.memory.written",
                "not-written": "timeline.memory.write.skipped",
            }[timeline_write["action"]]
            self.append_memory_audit(
                harness,
                event_type,
                "timeline-memory",
                {
                    "cardId": (card or {}).get("id"),
                    "cardIds": [item.get("id") for item in cards if item.get("id")],
                    "cardCount": len(cards),
                    "writePolicyMs": memory_write_ms,
                    "kind": (card or {}).get("kind") or memory_kind,
                    "tier": (card or {}).get("tier"),
                    "storageScope": (card or {}).get("scope")
                    or timeline_write.get("storageScope"),
                    "status": (card or {}).get("status")
                    or timeline_write.get("status"),
                    "committedCardIds": timeline_write.get("committedCardIds", []),
                    "memoryKey": (card or {}).get("memoryKey"),
                    "reason": timeline_write.get("reason"),
                    "fallback": timeline_write.get("fallback"),
                    "ignoredDraft": timeline_write.get("ignoredDraft"),
                    "evidenceEventId": transcript_event["eventId"],
                    "clock": clock_context.prompt_payload(),
                },
                parent_event_id=transcript_event["eventId"],
            )
        return (timeline_write, memory_requested, memory_kind, memory_consent)
