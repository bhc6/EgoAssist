from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from egoassist.prompts import compact_audit_payload
from egoassist.realtime import (
    CaseWorkLedger,
    TurnTakingPolicy,
    default_memory_policy,
    memory_profiles,
    normalize_memory_policy,
    platform_capability_truth,
)
from egoassist.realtime.context_harness import ContextHarness
from egoassist.realtime.episodes import (
    RealtimeEpisodeMemory,
    retrieval_audit_from_events,
)
from egoassist.realtime.locale_prompts import PROMPTS
from egoassist.realtime.performance import performance_snapshot
from egoassist.realtime.skills import SkillRuntime
from egoassist.realtime.tools import SkillToolExecutor


def compact_work_item(row):
    value = dict(row)
    if isinstance(value.get("semantic"), dict):
        value["semantic"] = compact_audit_payload("control_decision", {"decision": value["semantic"]})["decision"]
    if isinstance(value.get("payload"), dict) and isinstance(value["payload"].get("semantic"), dict):
        value["payload"] = {**value["payload"], "semantic": compact_audit_payload(
            "control_decision", {"decision": value["payload"]["semantic"]})["decision"]}
    return value


@dataclass
class StateProjection:
    ambient_snapshot: Any
    artifacts_for_runtime: Any
    capability_overrides: Any
    case_owners: Any
    context: Any
    default_interaction: Any
    episodic: Any
    episodic_for_user: Any
    human_help_port: Any
    ledger: Any
    live_bridges: Any
    media_configuration: Any
    memory_retrieval: Any
    model: Any
    product_database: Any
    projection_cache: Any
    runtime: Any
    runtime_skill_summaries: Any
    runtime_skill_summary: Any
    timeline: Any
    tools: Any
    diary_status: Any = None
    memory_metrics: Any = None
    memory_service: Any = None

    def state(
        self,
        target_runtime: SkillRuntime | None = None,
        *,
        routing_override: dict | None = None,
        bridge_override=None,
        harness_override: ContextHarness | None = None,
        tools_override: SkillToolExecutor | None = None,
        episodic_override: RealtimeEpisodeMemory | None = None,
        media_override: dict | None = None,
        user_scope: str = "local-default",
        memory_policy_override: dict | None = None,
        session_id_override: str | None = None,
    ):
        active_runtime = target_runtime or self.runtime
        active_routing = routing_override or self.default_interaction.routing
        active_tools = tools_override or self.tools
        active_episodic = episodic_override or self.episodic
        active_policy = normalize_memory_policy(
            memory_policy_override or default_memory_policy()
        )
        preference_settings = active_policy["components"]["timeline"]
        episodic_settings = active_policy["components"]["timeline"]
        harness_snapshot = harness_override.snapshot() if harness_override else None
        product_projection = None
        if harness_override is not None:
            journal_path = Path(harness_override.journal.path)
            try:
                stat = journal_path.stat()
                marker = (int(stat.st_mtime_ns), int(stat.st_size))
            except OSError:
                marker = (0, 0)
            cached = self.projection_cache.get(str(journal_path))
            if cached is None or cached[:2] != marker:
                self.projection_cache[str(journal_path)] = (
                    *marker,
                    self.product_database.project_journal(
                        harness_override.journal, user_id=user_scope
                    ),
                )
            product_projection = self.projection_cache[str(journal_path)][2]
        work_ledger_snapshot = (
            CaseWorkLedger(harness_override.journal).snapshot(limit=100, work_limit=20)
            if harness_override
            else {
                "turnInbox": [],
                "intents": [],
                "foregroundIntentId": None,
                "jobs": [],
                "resultInbox": [],
            }
        )
        if harness_override:
            work_ledger_snapshot["turnInbox"] = [compact_work_item(row) for row in work_ledger_snapshot["turnInbox"]]
            for key in ("intents", "jobs", "resultInbox"):
                value = work_ledger_snapshot.get(key)
                if isinstance(value, list):
                    work_ledger_snapshot[key] = [compact_work_item(row) for row in value[-20:]]
        visible_artifacts = self.artifacts_for_runtime(active_runtime, user_scope)
        human_help_summary = (
            self.human_help_port.public_summary()
            if self.human_help_port is not None
            else {"configured": False, "connector": None}
        )
        capability_catalog = {
            "platform": platform_capability_truth(self.capability_overrides),
            "skills": [
                {
                    "name": item["name"],
                    "capabilities": item.get("capabilityTruth", []),
                    "tools": item.get("tools", []),
                }
                for item in self.runtime_skill_summaries()
            ],
        }
        visible_events = (
            [
                {
                    "eventId": entry.get("id"),
                    "parentEventIds": [entry["parentId"]]
                    if entry.get("parentId")
                    else [],
                    "sessionId": harness_override.session_id,
                    "timestampWall": entry.get("timestamp"),
                    "type": entry.get("eventType")
                    if entry.get("type") == "runtime_event"
                    else entry.get("type"),
                    "source": entry.get("source", "context-journal"),
                    "payload": compact_audit_payload(
                        entry.get("eventType", entry.get("type")),
                        entry.get("payload", entry),
                    ),
                    "responseId": entry.get("responseId"),
                    "latencyMs": entry.get("latencyMs"),
                    "model": entry.get("model"),
                    "skill": entry.get("skill"),
                    "task": entry.get("task"),
                }
                for entry in harness_override.journal.entries(tail=160)
            ]
            if harness_override
            else self.ledger.snapshot()
            if user_scope == "local-default"
            else []
        )
        visible_session_id = (
            harness_override.session_id
            if harness_override
            else session_id_override
            if session_id_override
            else self.ledger.session_id
            if user_scope == "local-default"
            else None
        )
        preference_memory = (
            self.episodic_for_user(user_scope)
            if user_scope != "local-default"
            else active_episodic
        )
        memory_state = self.memory_service.snapshot_preference(preference_memory,
            candidate_scope=visible_session_id
        )
        compiled_context = self.context(active_runtime)
        bridges = (
            (bridge_override,)
            if bridge_override is not None
            else tuple(self.live_bridges)
            if user_scope == "local-default"
            else ()
        )
        scheduler = (
            bridges[0].scheduler_snapshot()
            if bridges and hasattr(bridges[0], "scheduler_snapshot")
            else {
                "pending": False,
                "activeResponseIds": [],
                "queued": None,
                "inflight": None,
                "activePolicies": [],
                "bargeInPending": False,
            }
        )
        provider_lanes = (
            bridges[0].provider_lanes_snapshot()
            if bridges and hasattr(bridges[0], "provider_lanes_snapshot")
            else {
                "dialogue": {
                    "connected": False,
                    "microphone": True,
                    "turnDetection": "server-vad-explicit-response",
                    "userVisibleOutput": True,
                },
                "perception": {
                    "connected": False,
                    "microphoneShared": False,
                    "turnDetection": "disabled-explicit-response",
                },
            }
        )
        owner = self.case_owners.get(visible_session_id)
        ambient_state = (
            self.ambient_snapshot(user_scope, visible_session_id)
            if harness_override
            else None
        )
        if owner and any(
            (
                row.get("draftId") and row["status"] in {"audio-sent", "played"}
                for row in owner.lanes.reviews.rows.values()
            )
        ):
            owner.lanes.reviews.reconcile(
                self.memory_service.pending_draft(
                    user_scope, visible_session_id
                )
            )
        runtime_lanes = (
            owner.lanes.snapshot(
                observation=ambient_state,
                scheduler=scheduler,
                pending_inputs=owner.turn_queue.qsize() if owner.turn_queue else 0,
                classifying=sum(
                    (not task.done() for task in owner.classification_tasks)
                ),
                consolidating=bool(
                    owner.maintenance_task and (not owner.maintenance_task.done())
                ),
                memory_enabled=bool(
                    episodic_settings["episodic_enabled"]
                    or preference_settings["preference_enabled"]
                ),
                account_diary=self.diary_status(user_scope) if self.diary_status else None,
                memory_metrics=self.memory_metrics(user_scope, visible_session_id) if self.memory_metrics else None,
            )
            if owner
            else None
        )
        default_policy = TurnTakingPolicy()
        audio_attribution = {
            "input": "microphone transcript -> acoustic speaker hypothesis; API user role does not identify wearer",
            "output": "provider audio_transcript -> assistant",
            "browserCapture": dict(media_override)
            if media_override is not None
            else self.media_configuration
            if user_scope == "local-default"
            else {},
            "echoControl": "browser-webrtc-aec",
            "claimBoundary": "Logical channels are distinct; residual acoustic echo is observable but speaker identity is not guaranteed.",
        }
        episodic_state = self.memory_service.snapshot_episode(active_episodic,
            session_id=visible_session_id,
            user_scope=user_scope,
            allow_cross_session=episodic_settings["scope"] == "participant",
            allow_participant_overlay=preference_settings["preference_enabled"],
        )
        saved_retrieval = retrieval_audit_from_events(visible_events)
        if saved_retrieval is not None:
            episodic_state["retrieval"] = saved_retrieval
        episodic_state = {
            **episodic_state,
            "caseTimelineEnabled": episodic_settings["episodic_enabled"],
            "participantTimelineEnabled": preference_settings["preference_enabled"],
            "timelineViews": {
                "case": self.timeline.snapshot(
                    user_id=user_scope,
                    case_id=visible_session_id or "local-default",
                    scope="case",
                ),
                "participant": self.timeline.snapshot(
                    user_id=user_scope, case_id=None, scope="participant"
                ),
            },
        }
        if not episodic_settings["episodic_enabled"] and (
            not preference_settings["preference_enabled"]
        ):
            episodic_state = {
                **episodic_state,
                "retrieval": {
                    "method": "disabled-by-memory-policy",
                    "query": "",
                    "scope": {
                        "user": user_scope,
                        "session": visible_session_id,
                        "allowCrossSession": episodic_settings["scope"]
                        == "participant",
                    },
                    "minimumScore": None,
                    "candidates": [],
                    "selected": [],
                    "injection": {
                        "status": "disabled",
                        "episodeIds": [],
                        "evidence": [],
                    },
                    "claimBoundary": "Behavioral timeline persistence, retrieval, and injection are disabled for this case.",
                },
            }
        if active_runtime.task is None:
            candidate = memory_state.get("candidate")
            generic_memory = {
                "policy": "explicit-confirmation"
                if preference_settings["preference_enabled"]
                else "disabled-by-memory-policy",
                "scope": {
                    "skill": active_runtime.skill.name
                    if active_runtime.skill
                    else None,
                    "task": None,
                    "contextAnchor": None,
                },
                "evidence": {
                    "status": "waiting",
                    "text": PROMPTS.diag_evidence_waiting,
                    "source": "realtime-session",
                },
                "candidate": {
                    "status": "pending" if candidate else "none",
                    "text": candidate.get("evidence")
                    if candidate
                    else PROMPTS.diag_candidate_none,
                },
                "confirmation": {
                    "status": "none",
                    "text": PROMPTS.diag_confirmation_none,
                    "count": 0,
                },
                "retrieval": {
                    "status": "no-match"
                    if preference_settings["preference_enabled"]
                    else "disabled",
                    "text": PROMPTS.diag_retrieval_waiting_scope
                    if preference_settings["preference_enabled"]
                    else PROMPTS.diag_retrieval_disabled,
                    **self.memory_retrieval(
                        active_runtime,
                        episodic_memory=active_episodic,
                        enabled=preference_settings["preference_enabled"],
                    ),
                },
                "impact": {"status": "none", "text": PROMPTS.diag_impact_none},
                "lastDecision": memory_state["decision"],
            }
            unresolved_task = bool(active_runtime.skill and active_runtime.tasks)
            generic_decision = {
                "action": "ask" if unresolved_task else "silent",
                "message": PROMPTS.diag_decision_waiting
                if unresolved_task
                else PROMPTS.diag_decision_general,
                "reason": "skill-loaded-task-unresolved"
                if unresolved_task
                else "conversational-skill"
                if active_runtime.skill
                else "no-domain-intent",
                "confidence": 1.0 if active_runtime.skill else 0.0,
            }
            return {
                "memory": memory_state,
                "memoryPolicy": active_policy,
                "memoryProfiles": memory_profiles(),
                "memoryMechanism": generic_memory,
                "task": None,
                "genericDecision": generic_decision,
                "realtimeModel": self.model,
                "providerLanes": provider_lanes,
                "activeSkill": self.runtime_skill_summary(active_runtime.skill)
                if active_runtime.skill
                else None,
                "skills": self.runtime_skill_summaries(),
                "tasks": active_runtime.task_summaries(),
                "intentRouting": active_routing,
                "audioAttribution": audio_attribution,
                "episodicMemory": episodic_state,
                "toolAudit": list(reversed(active_tools.audit[-8:])),
                "toolActivity": harness_override.journal.tool_activity()
                if harness_override
                else [],
                "toolApproval": "automatic",
                "ambient": ambient_state,
                "runtimeLanes": runtime_lanes,
                "events": visible_events,
                "performance": performance_snapshot(visible_events),
                "sessionId": visible_session_id,
                "scheduler": scheduler,
                "workLedger": work_ledger_snapshot,
                "interaction": self.case_owners[
                    visible_session_id
                ].interaction.snapshot()
                if visible_session_id in self.case_owners
                and self.case_owners[visible_session_id].interaction
                else None,
                "sharedScene": self.case_owners[visible_session_id].scene.snapshot()
                if visible_session_id in self.case_owners
                and self.case_owners[visible_session_id].scene
                else None,
                "artifacts": visible_artifacts,
                "humanHelp": human_help_summary,
                "productPersistence": {
                    "status": "projected" if product_projection is not None else "idle",
                    "projection": product_projection,
                    "source": "session-journal-compatible-projector",
                },
                "capabilityCatalog": capability_catalog,
                "turnTakingPolicy": {
                    **default_policy.public(),
                    "currentSelection": default_policy.select(generic_decision),
                    "source": "core-default",
                },
                "memorySystems": [
                    {
                        "id": "working",
                        "name": PROMPTS.diag_mem_working_name,
                        "status": "live" if bridges else "idle",
                        "implementation": PROMPTS.diag_mem_working_impl,
                    },
                    {
                        "id": "preference",
                        "name": PROMPTS.diag_mem_preference_name,
                        "status": "waiting-scope"
                        if preference_settings["preference_enabled"]
                        else "disabled",
                        "implementation": PROMPTS.diag_mem_preference_impl,
                    },
                    {
                        "id": "episodic",
                        "name": PROMPTS.diag_mem_episodic_name,
                        "status": "connected-summary"
                        if episodic_settings["episodic_enabled"]
                        else "disabled",
                        "implementation": PROMPTS.diag_mem_episodic_impl_no_task,
                    },
                    {
                        "id": "knowledge",
                        "name": PROMPTS.diag_mem_knowledge_name,
                        "status": "static-injection"
                        if active_runtime.skill
                        else "unloaded",
                        "implementation": "Active Skill policy and declared resources"
                        if active_runtime.skill
                        else PROMPTS.diag_mem_knowledge_impl_no_task,
                    },
                ],
                "knowledgeMechanism": {
                    "method": "static-skill-policy-injection-v1"
                    if active_runtime.skill
                    else "none",
                    "selected": {
                        "skill": active_runtime.skill.name,
                        "contentHash": active_runtime.skill.content_hash,
                    }
                    if active_runtime.skill
                    else None,
                    "candidates": [],
                    "claimBoundary": "Active Skill policy is injected; no procedural task is selected."
                    if active_runtime.skill
                    else "No domain knowledge is injected before intent routing.",
                },
                "promptContext": {
                    "hash": harness_snapshot.get("manifest", {}).get("contextHash")
                    if harness_snapshot
                    else hashlib.sha256(compiled_context.encode("utf-8")).hexdigest()[
                        :16
                    ],
                    "text": compiled_context,
                    "skill": active_runtime.skill.name
                    if active_runtime.skill
                    else None,
                    "task": None,
                    "taskVersion": None,
                    "harnesses": [harness_snapshot] if harness_snapshot else [],
                },
            }
        task = active_runtime.task
        spec = active_runtime.task.spec
        task_state = task.snapshot()
        retrieval = self.memory_retrieval(
            active_runtime,
            episodic_memory=active_episodic,
            enabled=preference_settings["preference_enabled"],
        )
        hint = retrieval["selected"]["record"] if retrieval["selected"] else None
        hint_was_used = bool(hint and hint["id"] in task.announced_memory_ids)
        task_records = [
            record
            for record in memory_state["records"]
            if record.get("trigger", {}).get("task") in {spec.id, spec.title}
        ]
        observation = task_state.get("observation") or {}
        candidate = memory_state.get("candidate")
        memory_mechanism = {
            "policy": "explicit-confirmation"
            if preference_settings["preference_enabled"]
            else "disabled-by-memory-policy",
            "scope": {
                "skill": active_runtime.skill.name,
                "task": spec.id,
                "contextAnchor": task.current_step.id,
            },
            "evidence": {
                "status": "observed" if observation else "waiting",
                "text": observation.get("summary") or PROMPTS.diag_evidence_waiting,
                "source": observation.get("source") or "realtime-session",
            },
            "candidate": {
                "status": "pending" if candidate else "none",
                "text": candidate.get("evidence")
                if candidate
                else PROMPTS.diag_candidate_none,
            },
            "confirmation": {
                "status": "confirmed" if task_records else "none",
                "text": PROMPTS.diag_task_confirmed_count.format(
                    count=len(task_records)
                ),
                "count": len(task_records),
            },
            "retrieval": {
                "status": "matched"
                if hint
                else "no-match"
                if preference_settings["preference_enabled"]
                else "disabled",
                "text": hint.get("prompt")
                if hint
                else PROMPTS.diag_task_no_match
                if preference_settings["preference_enabled"]
                else PROMPTS.diag_retrieval_disabled,
                "memoryId": hint.get("id") if hint else None,
                "method": retrieval["method"],
                "query": retrieval["query"],
                "candidates": retrieval["candidates"],
            },
            "impact": {
                "status": "reused" if hint_was_used else "ready" if hint else "none",
                "text": PROMPTS.diag_impact_reused
                if hint_was_used
                else PROMPTS.diag_impact_ready
                if hint
                else PROMPTS.diag_impact_none,
            },
            "lastDecision": memory_state["decision"],
        }
        return {
            "memory": memory_state,
            "memoryPolicy": active_policy,
            "memoryProfiles": memory_profiles(),
            "memoryMechanism": memory_mechanism,
            "task": task_state,
            "realtimeModel": self.model,
            "providerLanes": provider_lanes,
            "activeSkill": self.runtime_skill_summary(active_runtime.skill),
            "skills": self.runtime_skill_summaries(),
            "tasks": active_runtime.task_summaries(),
            "intentRouting": active_routing,
            "audioAttribution": audio_attribution,
            "episodicMemory": episodic_state,
            "toolAudit": list(reversed(active_tools.audit[-8:])),
            "toolActivity": harness_override.journal.tool_activity()
            if harness_override
            else [],
            "toolApproval": "automatic",
            "ambient": ambient_state,
            "runtimeLanes": runtime_lanes,
            "events": visible_events,
            "performance": performance_snapshot(visible_events),
            "sessionId": visible_session_id,
            "scheduler": scheduler,
            "workLedger": work_ledger_snapshot,
            "interaction": self.case_owners[visible_session_id].interaction.snapshot()
            if visible_session_id in self.case_owners
            and self.case_owners[visible_session_id].interaction
            else None,
            "sharedScene": self.case_owners[visible_session_id].scene.snapshot()
            if visible_session_id in self.case_owners
            and self.case_owners[visible_session_id].scene
            else None,
            "artifacts": visible_artifacts,
            "humanHelp": human_help_summary,
            "capabilityCatalog": capability_catalog,
            "turnTakingPolicy": {
                **TurnTakingPolicy.from_manifest(
                    active_runtime.skill.manifest
                ).public(),
                "currentSelection": TurnTakingPolicy.from_manifest(
                    active_runtime.skill.manifest
                ).select(task.decision),
                "source": f"skill:{active_runtime.skill.name}",
            },
            "memorySystems": [
                {
                    "id": "working",
                    "name": PROMPTS.diag_mem_working_name,
                    "status": "live" if bridges else "idle",
                    "implementation": PROMPTS.diag_mem_working_impl,
                },
                {
                    "id": "preference",
                    "name": PROMPTS.diag_mem_preference_name,
                    "status": "connected"
                    if preference_settings["preference_enabled"]
                    else "disabled",
                    "implementation": PROMPTS.diag_mem_preference_impl,
                },
                {
                    "id": "episodic",
                    "name": PROMPTS.diag_mem_episodic_name,
                    "status": "connected-summary"
                    if episodic_settings["episodic_enabled"]
                    else "disabled",
                    "implementation": PROMPTS.diag_mem_episodic_impl_with_task,
                },
                {
                    "id": "knowledge",
                    "name": PROMPTS.diag_mem_knowledge_name,
                    "status": "static-injection",
                    "implementation": PROMPTS.diag_mem_knowledge_impl_with_task,
                },
            ],
            "knowledgeMechanism": {
                "method": "static-skill-task-injection-v1",
                "selected": {
                    "checkpoint": task.current_step.id,
                    "source": task.current_step.source,
                },
                "candidates": [
                    {
                        "checkpoint": item.id,
                        "source": item.source,
                        "selected": item.id == task.current_step.id,
                    }
                    for item in spec.steps
                ],
                "claimBoundary": PROMPTS.diag_claim_boundary,
            },
            "promptContext": {
                "hash": harness_snapshot.get("manifest", {}).get("contextHash")
                if harness_snapshot
                else hashlib.sha256(compiled_context.encode("utf-8")).hexdigest()[:16],
                "text": compiled_context,
                "skill": active_runtime.skill.name,
                "task": spec.id,
                "taskVersion": spec.version,
                "harnesses": [harness_snapshot] if harness_snapshot else [],
            },
        }
