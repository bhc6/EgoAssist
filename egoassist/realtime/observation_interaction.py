"""Interaction decisions triggered by validated observations.

Only this consumer mutates task state or proposes speech. The media adapter
publishes its result after the case commit lock has been released.
"""
from dataclasses import dataclass
from typing import Any
from egoassist.core.lane_contracts import AcceptedObservation
from egoassist.core.settings import observation_speech_max_age_ms
from egoassist.memory.service import ObservationMemory
from egoassist.prompts import prompt_text

@dataclass
class ObservationInteraction:
    ambient_options: Any
    apply_memory_hint: Any
    bridge: Any
    clock_for_session: Any
    current_episodic_memory: Any
    current_memory_policy: Any
    interaction_state: Any
    journal: Any
    owner: Any
    request_timeline_consolidation: Any
    runtime_checkpoint: Any
    session_id: str
    session_runtime: Any
    session_trace: Any
    memory_service: Any
    user_id: str

    def remember(self, message, *, ambient):
        captured, hypothesis, evidence = message.captured, message.hypothesis, message.evidence
        eligible = ((hypothesis.observed if captured.observation_mode == 'focused' else bool(hypothesis.visible_evidence))
            and (captured.previous_observation or {}).get('visibleEvidence') != hypothesis.visible_evidence) if ambient else True
        request = ObservationMemory(self.user_id, self.session_id, message.entry['id'],
            '；'.join(hypothesis.visible_evidence) if ambient else str(evidence.evidence.get('summary', '')),
            evidence.source, captured.frame_captured_at.isoformat(), evidence.received_at.isoformat(),
            self.clock_for_session(self.user_id, self.session_id),
            {'frame': getattr(captured, 'media', None), 'observationId': evidence.observation_id,
             'uncertainties': getattr(hypothesis, 'uncertainties', []), 'claim': 'visual-observation'},
            eligible=eligible, skill=self.session_runtime.skill.name if self.session_runtime.skill else None,
            task=evidence.task_id)
        try:
            card = self.memory_service.remember_observation(request, self.current_memory_policy())
        except Exception as exc:
            self.journal.append('memory.write.failed', {'sourceEventId': message.entry['id'],
                'operation': 'observation', 'error': type(exc).__name__}, required_event_ids=(message.entry['id'],))
            return None
        if card:
            self.session_trace('timeline.memory.written', 'observation-memory',
                {'cardId': card['id'], 'observationId': evidence.observation_id,
                 'frameId': captured.frame_id, 'happenedAt': captured.frame_captured_at.isoformat()})
            self.request_timeline_consolidation(self.owner)
        return card

    def ambient(self, message: AcceptedObservation):
        captured, hypothesis, evidence = message.captured, message.hypothesis, message.evidence
        observation_entry = message.entry
        fresh_for_speech = evidence.frame_age_ms <= observation_speech_max_age_ms(hypothesis.notification)
        self.remember(message, ambient=True)
        self.owner.scene.events.schedule()
        speech = None
        try:
            pending = self.memory_service.pending_draft(self.user_id, self.session_id)
        except Exception as exc:
            self.journal.append('memory.read', {'status': 'degraded',
                'failures': {'pendingDraft': type(exc).__name__}})
            return {'accepted': True, 'reason': message.reason, 'speech': None,
                    'memoryStatus': 'unavailable'}
        scheduler = (
            self.bridge().scheduler_snapshot()
            if hasattr(self.bridge(), "scheduler_snapshot")
            else {}
        )
        playback = self.interaction_state.playback_context() or {}
        busy = bool(
            self.owner.queued_turn_ids
            or scheduler.get("pending")
            or scheduler.get("activeResponseIds")
            or scheduler.get("queued")
            or (playback.get("generatedMs", 0) > playback.get("playedMs", 0))
            or (
                hasattr(self.bridge(), "has_unresolved_user_input")
                and self.bridge().has_unresolved_user_input()
            )
        )
        review = self.owner.lanes.reviews.consider(
            captured,
            hypothesis,
            observation_entry,
            fresh=fresh_for_speech,
            enabled=self.ambient_options(self.owner)["questionsEnabled"],
            busy=busy,
            allowed=self.interaction_state.allows({}, "proactive-utterance"),
            pending=pending,
            cooldown=self.ambient_options(self.owner)["questionCooldownSeconds"],
            reminders_enabled=self.ambient_options(self.owner).get("remindersEnabled", True),
        )
        if review.get("notification"):
            goal = self.owner.lanes.observation_goal()
            current_goals = [goal["sourceId"]] if goal else []
            if review["reminderGoalSourceIds"] != current_goals:
                review = self.owner.lanes.reviews.record(review, status="deferred", reason="reminder-goal-changed")
        self.owner.scene.ingest_review(review)
        if review["status"] == "ready":
            speech = {
                "utterance": prompt_text(
                    "perception/ambient_question", question=hypothesis.clarification
                ),
                "reason": "clarification",
            } if not review.get("notification") else {
                "utterance": prompt_text("perception/ambient_notification", message=review["notification"]["message"]),
                "reason": "observation-reminder",
            }
            if (review.get('notification') or {}).get('kind') == 'requested-reminder':
                speech['utterance'] += ' 这是刚才采样画面中的事件。用“刚才看到”表述，不声称此刻仍在发生；直接完成用户已请求的提醒，不询问是否需要提醒，不添加记录或保存承诺。'
        return {
            "accepted": True,
            "reason": message.reason,
            "speech": speech,
            "origin": {
                "observationReviewId": review["candidateId"],
                "observationKind": "reminder" if review.get("notification") else "clarification",
                "generation": self.owner.output_generation,
                "watchlistId": captured.watchlist_id,
                "watchlistVersion": captured.watchlist_version,
                "watchEpoch": captured.watch_epoch,
                "contextId": captured.context_id,
                "frameId": captured.frame_id,
                "observed": hypothesis.observed,
                "activityId": hypothesis.activity_id,
            },
        }

    def reject_task(self, reason, captured, **detail):
        task = self.session_runtime.task
        if task:
            task.record_perception_rejection(reason, frame_id=captured.frame_id,
                context_id=captured.context_id, detail=detail)
            self.journal.append_state_checkpoint(self.runtime_checkpoint(self.session_runtime),
                                                reason='perception-evidence-rejected')
        return {'accepted': False, 'reason': reason}

    def task(self, message: AcceptedObservation):
        captured, evidence = message.captured, message.evidence
        active_task = self.session_runtime.task
        active_task.ingest(evidence.model_dump(mode='json'))
        self.remember(message, ambient=False)
        policy = self.current_memory_policy()
        try:
            self.apply_memory_hint(self.session_runtime,
                episodic_memory=self.current_episodic_memory(policy),
                enabled=policy['components']['timeline']['preference_enabled'])
        except Exception as exc:
            self.journal.append('memory.read', {'status': 'degraded',
                'failures': {'taskHint': type(exc).__name__}})
        self.journal.append_state_checkpoint(self.runtime_checkpoint(self.session_runtime),
                                            reason='task-bound-perception-evidence')
        task_snapshot = active_task.snapshot()
        observation = task_snapshot.get("observation") or {}
        speech = None
        if observation.get("hazards"):
            speech = {
                "reason": "hazard",
                "utterance": observation["hazards"][0]["message"],
            }
        elif (
            observation.get("completed")
            and observation.get("step") == task_snapshot.get("currentCheckpointId")
            and observation.get("temporalConsistency", {}).get("consistent")
        ):
            speech = {
                "reason": "confirmation",
                "utterance": task_snapshot["decision"]["message"],
            }
        elif (
            observation.get("step")
            not in {"unknown", task_snapshot.get("currentCheckpointId")}
            and observation.get("confidence", 0) >= 0.65
        ):
            speech = {
                "reason": "path-deviation",
                "utterance": task_snapshot["decision"]["message"],
            }
        return {
            "accepted": True,
            "reason": message.reason,
            "speech": speech,
            "origin": {
                "taskId": evidence.task_id,
                "taskVersion": captured.task_version,
                "checkpointId": captured.checkpoint_id,
                "checkpointEpoch": captured.checkpoint_epoch,
                "contextId": captured.context_id,
                "frameId": captured.frame_id,
                "observationId": evidence.observation_id,
            },
        }
