from __future__ import annotations

from egoassist.core.source_contract import SourceReference

import time
from dataclasses import dataclass
from copy import deepcopy
from egoassist.memory.service import MemoryQuery, PreparedMemory
from typing import Any
from uuid import uuid4

from egoassist.memory import ClockContext
from egoassist.memory.timeline import DEFAULT_RETRIEVAL_MIN_SCORE
from egoassist.realtime import default_memory_policy, normalize_memory_policy
from egoassist.realtime.context_harness import ContextHarness
from egoassist.realtime.episodes import RealtimeEpisodeMemory
from egoassist.realtime.locale_prompts import PROMPTS
from egoassist.realtime.skills import SkillRuntime
from egoassist.realtime.router_context import retrieval_text


@dataclass
class DefaultInteractionState:
    routing: Any = None
    episode_injection: Any = None


@dataclass
class InteractionService:
    append_memory_audit: Any
    apply_memory_hint: Any
    case_owners: Any
    default_interaction: Any
    episodic: Any
    episodic_for_user: Any
    find_memory_hint: Any
    live_bridges: Any
    memory_service: Any
    memory_retrieval: Any
    registry: Any
    runtime: Any
    runtime_checkpoint: Any
    runtime_context: Any
    trace_event: Any

    def process_transcript(
        self,
        transcript: str,
        harness: ContextHarness | None = None,
        target_runtime: SkillRuntime | None = None,
        *,
        turn_id: str | None = None,
        transcript_entry: dict | None = None,
        persist_runtime_checkpoint: bool = True,
        user_scope: str = "local-default",
        semantic_decision: dict | None = None,
        memory_policy: dict | None = None,
        episodic_memory: RealtimeEpisodeMemory | None = None,
        clock_context: ClockContext | None = None,
        project_retrieval: bool = True,
        prepared_memory: PreparedMemory | None = None,
        retrieval_semantic: dict | None = None,
    ) -> dict:
        transcript = str(transcript).strip()
        if not transcript:
            raise ValueError("transcript text cannot be empty")
        active_runtime = target_runtime or self.runtime
        active_episodic = episodic_memory or self.episodic
        active_policy = normalize_memory_policy(
            memory_policy or default_memory_policy()
        )
        preference_enabled = active_policy["components"]["timeline"][
            "preference_enabled"
        ]
        episodic_settings = active_policy["components"]["timeline"]
        clock_context = clock_context or ClockContext.current()
        if transcript_entry:
            clock_context = clock_context.at_source(
                (transcript_entry.get("metadata") or {}).get("sourceRecordedAt") or transcript_entry.get("timestamp"))
        if harness:
            transcript_entry = transcript_entry or harness.record_user(
                transcript,
                source="input-audio-transcript",
                turn_id=turn_id or uuid4().hex[:16],
            )
            transcript_event = {"eventId": transcript_entry["id"]}
        else:
            transcript_event = self.trace_event(
                "audio.transcript.completed", "realtime-user", {"text": transcript}
            )
        semantic_supplied = semantic_decision is not None
        semantic_decision = dict(semantic_decision or {})
        semantic_method = str(
            semantic_decision.get("method") or "semantic-turn-router-v4"
        )
        speech_control = semantic_decision.get("speechControl")
        task_control = semantic_decision.get("taskControl")
        skill_control = semantic_decision.get("skillControl")
        selected_by_semantic = semantic_decision.get("selectedSkill")
        retrieval_started = time.perf_counter()
        read_semantic = semantic_decision if retrieval_semantic is None else retrieval_semantic
        query = MemoryQuery(user_scope, harness.session_id if harness else None, transcript,
            active_policy, clock_context, deepcopy(read_semantic), retrieval_text(transcript, read_semantic),
            (harness.session_id + ':' + transcript_entry['id'],) if harness else (),
            SourceReference.from_entry(user_scope, harness.session_id, transcript_entry) if harness else None)
        prepared_memory = prepared_memory or self.memory_service.read(query)
        prepared_memory = self.memory_service.accept(prepared_memory, query, harness.journal if harness else None)
        episodic_retrieval = prepared_memory.retrieval
        durable_retrieval = prepared_memory.participant
        if episodic_retrieval is not None:
            turn_episode_injection = active_episodic.mark_injected(
                episodic_retrieval, project=project_retrieval
            )
        else:
            episodic_retrieval = {
                "method": "memory-read-unavailable" if prepared_memory.failures else "disabled-by-memory-policy",
                "query": transcript,
                "referential": False,
                "scope": {
                    "user": user_scope,
                    "session": harness.session_id if harness else None,
                    "allowCrossSession": episodic_settings["scope"] == "participant",
                },
                "minimumScore": None,
                "candidates": [],
                "selected": [],
                "injection": {"status": "unavailable" if prepared_memory.failures else "disabled", "episodeIds": [], "evidence": []},
                "claimBoundary": "Memory read unavailable; absence of retrieved evidence does not establish absence of history." if prepared_memory.failures else "Episodic persistence, retrieval, and injection are disabled for this case.",
            }
            turn_episode_injection = {
                "status": "unavailable" if prepared_memory.failures else "disabled",
                "query": transcript,
                "episodeIds": [],
                "evidence": [],
            }
        if harness:
            self.append_memory_audit(
                harness,
                "episodic.memory.retrieved",
                "realtime-episodic-memory",
                {
                    "enabled": bool(
                        episodic_settings["episodic_enabled"]
                        or durable_retrieval
                    ),
                    "caseMemoryEnabled": episodic_settings["episodic_enabled"],
                    "participantMemoryEnabled": preference_enabled,
                    "method": episodic_retrieval["method"],
                    "query": episodic_retrieval["query"],
                    "rankingQuery": episodic_retrieval.get("rankingQuery", transcript),
                    "referential": episodic_retrieval.get("referential", False),
                    "scope": episodic_retrieval["scope"],
                    "minimumScore": episodic_retrieval.get("minimumScore"),
                    "retrievalMs": round(
                        (prepared_memory.read_ms or 0) + (time.perf_counter() - retrieval_started) * 1000, 3
                    ),
                    "routing": episodic_retrieval.get("routing"),
                    "candidates": [
                        {
                            "episodeId": item["episodeId"],
                            "score": item["score"],
                            "selected": item["selected"],
                            "rejectionReasons": item.get("rejectionReasons", []),
                            "retrievalReasons": item.get("retrievalReasons", []),
                            "temporalSupport": item.get("temporalSupport"),
                        }
                        for item in episodic_retrieval.get("candidateAudit", episodic_retrieval.get("candidates", []))
                    ],
                },
                parent_event_id=transcript_event["eventId"],
            )
            self.append_memory_audit(
                harness,
                "episodic.memory.injected",
                "realtime-episodic-memory",
                turn_episode_injection,
                parent_event_id=transcript_event["eventId"],
            )
        if target_runtime is None:
            self.default_interaction.episode_injection = turn_episode_injection
        if harness and turn_episode_injection.get("evidence"):
            harness.journal.append_retrieval_evidence(
                transcript,
                turn_episode_injection["evidence"],
                turn_id=transcript_event["eventId"],
            )
        lexical_routing = self.registry.route(transcript)
        turn_routing = lexical_routing
        if skill_control == "deactivate":
            turn_routing = {
                **turn_routing,
                "method": semantic_method,
                "status": "deactivated" if active_runtime.skill else "no-match",
                "selectedSkill": None,
                "selectedTask": None,
                "skillControl": "deactivate",
                "claimBoundary": semantic_decision.get(
                    "claimBoundary",
                    "Structured semantic Skill control; no transcript phrase rule.",
                ),
            }
        elif speech_control:
            turn_routing = {
                **turn_routing,
                "method": semantic_method,
                "status": "suppressed-control",
                "selectedSkill": None,
                "selectedTask": None,
                "suppressedBy": speech_control,
                "claimBoundary": semantic_decision.get(
                    "claimBoundary",
                    "Structured semantic speech control; no transcript phrase rule.",
                ),
            }
        elif semantic_supplied and selected_by_semantic:
            activation_accepted = self.registry.accepts_automatic_activation(
                selected_by_semantic, lexical_routing
            )
            selected_task = (
                lexical_routing.get("selectedTask")
                if lexical_routing.get("selectedSkill") == selected_by_semantic
                else None
            )
            turn_routing = {
                **lexical_routing,
                "method": semantic_method + "+manifest-evidence",
                "status": "matched"
                if activation_accepted
                else "unverified-domain-intent",
                "proposedSkill": selected_by_semantic,
                "selectedSkill": selected_by_semantic if activation_accepted else None,
                "selectedTask": selected_task,
                "claimBoundary": "Semantic Skill proposal validated against the package activation policy; manifest evidence may select a task. Neither output is a calibrated probability.",
            }
            if not activation_accepted:
                selected_by_semantic = None
        elif semantic_supplied:
            turn_routing = {
                **lexical_routing,
                "method": semantic_method + "+manifest-evidence",
                "status": semantic_decision.get("skillStatus", "unavailable"),
                "selectedSkill": None,
                "selectedTask": None,
                "claimBoundary": semantic_decision.get(
                    "claimBoundary",
                    "Semantic routing unavailable; fail closed without changing Skill or task.",
                ),
            }
        if target_runtime is None:
            self.default_interaction.routing = turn_routing
        if harness:
            route_entry = harness.journal.append(
                "routing_decision",
                {
                    "routing": turn_routing,
                    "source": "skill-router",
                    "parentEventIds": [transcript_event["eventId"]],
                },
            )
            route_event = {"eventId": route_entry["id"]}
        else:
            route_event = self.trace_event(
                "intent.route.decision",
                "skill-router",
                turn_routing,
                [transcript_event["eventId"]],
            )
        selected_skill = (
            None
            if speech_control or skill_control == "deactivate"
            else selected_by_semantic
            if semantic_supplied
            else turn_routing.get("selectedSkill")
        )
        selected_task = (
            turn_routing.get("selectedTask")
            if selected_skill and turn_routing.get("selectedSkill") == selected_skill
            else None
        )
        runtime_changed = False
        matched_task = None
        if skill_control == "deactivate":
            before = self.runtime_checkpoint(active_runtime)
            previous_skill = before["skill"]
            active_runtime.deactivate()
            if target_runtime is None:
                self.runtime_context.update(skill=None, task=None)
            action = "deactivated" if previous_skill else "already-general"
            lifecycle_payload = {
                "action": action,
                "before": before,
                "after": self.runtime_checkpoint(active_runtime),
            }
            if harness:
                harness.journal.append(
                    "runtime_event",
                    {
                        "eventType": f"runtime.skill.{action}",
                        "source": "semantic-turn-router",
                        "payload": lifecycle_payload,
                        "model": semantic_decision.get("model"),
                        "skill": None,
                        "task": None,
                        "responseId": None,
                        "latencyMs": semantic_decision.get("latencyMs"),
                    },
                    parent_id=route_event["eventId"],
                )
            else:
                self.trace_event(
                    f"runtime.skill.{action}",
                    "semantic-turn-router",
                    lifecycle_payload,
                    [route_event["eventId"]],
                )
            runtime_changed = previous_skill is not None
        skill_changed = bool(
            selected_skill
            and (
                not active_runtime.skill or active_runtime.skill.name != selected_skill
            )
        )
        task_changed = bool(
            selected_task
            and (
                not active_runtime.task or active_runtime.task.spec.id != selected_task
            )
        )
        if selected_skill and (
            skill_changed or task_changed or active_runtime.task is None
        ):
            if not active_runtime.skill or active_runtime.skill.name != selected_skill:
                previous_skill = (
                    active_runtime.skill.name if active_runtime.skill else None
                )
                active_runtime.activate(selected_skill, select_default=False)
                if target_runtime is None:
                    self.runtime_context.update(skill=selected_skill, task=None)
                action = "switched" if previous_skill else "activated"
                lifecycle_payload = {
                    "action": action,
                    "previousSkill": previous_skill,
                    "skill": selected_skill,
                    "task": selected_task,
                }
                if harness:
                    harness.journal.append(
                        "runtime_event",
                        {
                            "eventType": f"runtime.skill.{action}",
                            "source": "semantic-turn-router",
                            "payload": lifecycle_payload,
                            "model": semantic_decision.get("model"),
                            "skill": selected_skill,
                            "task": selected_task,
                            "responseId": None,
                            "latencyMs": semantic_decision.get("latencyMs"),
                        },
                        parent_id=route_event["eventId"],
                    )
                else:
                    self.trace_event(
                        f"runtime.skill.{action}",
                        "semantic-turn-router",
                        lifecycle_payload,
                        [route_event["eventId"]],
                    )
                runtime_changed = True
            if selected_task and selected_task in active_runtime.tasks:
                active_runtime.select_task(selected_task)
                if target_runtime is None:
                    self.runtime_context["task"] = selected_task
                matched_task = selected_task
                runtime_changed = True
                if harness:
                    harness.journal.append(
                        "runtime_event",
                        {
                            "eventType": "runtime.task.selected",
                            "source": "semantic-turn-router",
                            "payload": {"task": selected_task},
                            "model": semantic_decision.get("model"),
                            "skill": selected_skill,
                            "task": selected_task,
                            "responseId": None,
                            "latencyMs": semantic_decision.get("latencyMs"),
                        },
                        parent_id=route_event["eventId"],
                    )
                else:
                    self.trace_event(
                        "runtime.task.selected",
                        "semantic-turn-router",
                        {"task": selected_task},
                        [route_event["eventId"]],
                    )
        timeline_write, memory_requested, memory_kind, memory_consent = (
            self.memory_service.commit(
                active_episodic=active_episodic,
                skill=active_runtime.skill.name if active_runtime.skill else None,
                task=active_runtime.task.spec.id if active_runtime.task else None,
                clock_context=clock_context,
                episodic_settings=episodic_settings,
                journal=harness.journal if harness else None,
                preference_enabled=preference_enabled,
                semantic_decision=semantic_decision,
                semantic_method=semantic_method,
                transcript=transcript,
                transcript_event=transcript_event,
                turn_id=turn_id,
                user_scope=user_scope,
            )
        )
        if active_runtime.task and (not speech_control):
            active_runtime.task.apply_voice_intent(task_control, transcript)
        if (
            active_runtime.task
            and preference_enabled
            and (not speech_control)
            and (semantic_decision.get("preferenceIntent") == "propose-durable")
            and (
                not (
                    memory_kind == "preference"
                    and memory_consent == "explicit"
                    and (
                        (timeline_write or {}).get("card", {}).get("status") == "active"
                    )
                )
            )
        ):
            step = active_runtime.task.current_step
            proposal = self.memory_service.propose_preference(active_episodic,
                task=active_runtime.task.spec.id,
                step=step.id,
                prompt=transcript[:160],
                preference=transcript[:160],
                source_quote=transcript[:160],
                evidence=f"用户明确提出长期偏好：{transcript[:160]}",
                scope_reason=f"话语发生时活动范围为 task={active_runtime.task.spec.id}, checkpoint={step.id}",
                candidate_scope=harness.session_id if harness else None,
            )
            if harness:
                self.append_memory_audit(
                    harness,
                    "memory.candidate.proposed",
                    "preference-memory",
                    {
                        **dict(proposal["candidate"] or {}),
                        "storageScope": active_policy["components"]["timeline"][
                            "scope"
                        ],
                    },
                    parent_event_id=transcript_event["eventId"],
                )
        hint, recalled = (
            self.apply_memory_hint(
                active_runtime,
                episodic_memory=active_episodic,
                enabled=preference_enabled,
            )
            if harness or self.live_bridges
            else (
                self.find_memory_hint(
                    target_runtime=active_runtime,
                    episodic_memory=active_episodic,
                    enabled=preference_enabled,
                ),
                False,
            )
        )
        if harness and persist_runtime_checkpoint:
            harness.journal.append_state_checkpoint(
                self.runtime_checkpoint(active_runtime), reason="user-turn"
            )
        preference_retrieval = self.memory_retrieval(
            active_runtime,
            emit=False,
            episodic_memory=active_episodic,
            enabled=preference_enabled,
        )
        if harness:
            self.append_memory_audit(
                harness,
                "memory.retrieval.completed",
                "preference-memory",
                {
                    "enabled": preference_enabled,
                    "storageScope": active_policy["components"]["timeline"]["scope"],
                    "method": preference_retrieval["method"],
                    "query": preference_retrieval["query"],
                    "candidateCount": len(preference_retrieval["candidates"]),
                    "selectedMemoryId": preference_retrieval["selected"]["memoryId"]
                    if preference_retrieval["selected"]
                    else None,
                    "candidates": [
                        {
                            "memoryId": item["memoryId"],
                            "selected": item["selected"],
                            "score": item["score"],
                            "rejectionReasons": item["rejectionReasons"],
                        }
                        for item in preference_retrieval["candidates"]
                    ],
                },
                parent_event_id=transcript_event["eventId"],
            )
        if harness and hint:
            harness.journal.append_retrieval_evidence(
                "confirmed preference for current task checkpoint",
                [
                    {
                        "memoryId": hint.get("id"),
                        "prompt": hint.get("prompt"),
                        "source": "confirmed-preference",
                    }
                ],
                turn_id=f"preference:{transcript_event['eventId']}",
            )
        if harness:
            turn_context_evidence = list(turn_episode_injection.get("evidence", []))
            if hint:
                turn_context_evidence.append(
                    {
                        "memoryId": hint.get("id"),
                        "prompt": hint.get("prompt"),
                        "source": "confirmed-preference",
                    }
                )
            harness.journal.append_turn_context(
                transcript, turn_context_evidence, turn_id=transcript_event["eventId"]
            )
        decision = (
            active_runtime.task.decision
            if active_runtime.task
            else {
                "action": "ask" if active_runtime.skill else "guide",
                "message": "请确认具体目标" if active_runtime.skill else "通用对话",
                "reason": "task-unresolved"
                if active_runtime.skill
                else "no-skill-required",
                "confidence": 1.0 if active_runtime.skill else 0.0,
            }
        )
        if speech_control == "stop-speaking":
            decision = {
                "action": "silent",
                "message": "停止当前语音输出",
                "reason": "semantic-stop-speaking-control",
                "confidence": 1.0,
            }
        committed_card_ids = list((timeline_write or {}).get("committedCardIds") or [])
        if committed_card_ids:
            committed_cards = [
                card
                for card in (timeline_write or {}).get("cards", [])
                if card.get("id") in committed_card_ids
            ]
            memory_response = {
                "requested": memory_requested,
                "status": "committed",
                "committedCardIds": committed_card_ids,
                "message": PROMPTS.memory_saved_prefix
                + "；".join(
                    (
                        str(card.get("text") or card.get("summary") or "")
                        for card in committed_cards
                    )
                )[:600]
                if memory_requested
                else None,
            }
        elif memory_requested:
            needs_value = (timeline_write or {}).get("action") == "capture"
            memory_response = {
                "requested": True,
                "status": "needs-input" if needs_value else "not-committed",
                "committedCardIds": [],
                "message": PROMPTS.memory_needs_input
                if needs_value
                else PROMPTS.memory_not_committed,
            }
        else:
            memory_response = {
                "requested": False,
                "status": "not-requested",
                "committedCardIds": [],
                "message": None,
            }
        evidence_package = prepared_memory.evidence
        if harness:
            harness.journal.append('memory.read', {'turnId': turn_id,
                'status': 'degraded' if prepared_memory.failures else 'completed',
                'failures': prepared_memory.failures, 'readMs': prepared_memory.read_ms})
            if evidence_package:
                from egoassist.product.answer_service import source_event_ids
                harness.journal.append('evidence.package', {'turnId': turn_id, 'packet': evidence_package},
                    required_event_ids=source_event_ids(harness.journal, evidence_package))
        return {
            "matchedTask": matched_task,
            "runtimeChanged": runtime_changed,
            "decision": decision,
            "memoryHint": hint,
            "memoryRecalled": recalled,
            "episodicRetrieval": episodic_retrieval,
            "episodicInjection": turn_episode_injection,
            "evidencePackage": evidence_package,
            "memoryPolicy": active_policy,
            "memoryRead": {"status": "degraded" if prepared_memory.failures else "completed", "failures": prepared_memory.failures},
            "routingState": turn_routing,
            "control": speech_control,
            "taskControl": task_control,
            "skillControl": skill_control,
            "semanticDecision": semantic_decision,
            "timelineWrite": timeline_write,
            "memoryResponse": memory_response,
        }
