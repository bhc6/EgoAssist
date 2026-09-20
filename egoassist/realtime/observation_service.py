from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from egoassist.core.settings import perception_settings
from egoassist.product import (
    AmbientFrameContext,
    AmbientObservationHypothesis,
    PerceptionFrameContext,
    PerceptionHypothesis,
)
from egoassist.prompts import prompt_text
from egoassist.realtime import validate_ambient_result, validate_perception_result
from egoassist.realtime.ambient import AMBIENT_SCHEMA_VERSION


@dataclass
class ObservationService:
    ambient_options: Any
    ambient_watch_session: Any
    apply_memory_hint: Any
    bridge: Any
    clock_for_session: Any
    current_episodic_memory: Any
    current_memory_policy: Any
    interaction_state: Any
    journal: Any
    model: Any
    owner: Any
    perception_context_for_runtime: Any
    product_database: Any
    request_timeline_consolidation: Any
    runtime_checkpoint: Any
    scoped_state_for_user: Any
    session_id: Any
    session_runtime: Any
    session_trace: Any
    timeline: Any
    user_id: Any
    ws: Any

    def session_perception_context(
        self, frame_id: str, captured_at: datetime
    ) -> dict[str, Any] | None:
        value = self.perception_context_for_runtime(
            self.session_runtime,
            user_id=self.user_id,
            session_id=self.session_id,
            frame_id=frame_id,
            captured_at=captured_at,
        )
        return value.model_dump(mode="python") if value is not None else None

    def session_ambient_context(
        self, frame_id: str, captured_at: datetime
    ) -> dict[str, Any] | None:
        watch = self.ambient_watch_session(self.owner)
        if watch is None:
            return None
        if not watch.should_submit(frame_id):
            return None
        context = watch.frame_context(
            user_id=self.user_id,
            session_id=self.session_id,
            frame_id=frame_id,
            captured_at=captured_at,
        ).model_dump(mode="python")
        context["reference_context"] = self.owner.lanes.observation_references(
            self.timeline,
            task=self.session_runtime.task.snapshot()
            if self.session_runtime.task
            else None,
        )
        frame = getattr(self.bridge(), "_latest_perception_frame", None)
        if frame and frame.get("frameId") == frame_id and self.owner.scene.frames:
            import base64

            data = base64.b64decode(frame["image"], validate=True)
            media = self.owner.scene._save(data, ".jpg", "image/jpeg")
            context["media"] = {
                **media,
                "frameId": frame_id,
                "capturedAt": captured_at.isoformat(),
            }
            self.journal.append(
                "ambient.frame",
                {
                    "frame": context["media"],
                    "contextId": context["context_id"],
                    "referenceContext": context["reference_context"],
                },
            )
        return context

    async def session_ambient_result(
        self, captured_value: dict[str, Any], raw_value: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle one ambient frame independently of the current task.

        Rejections are journalled rather than recorded on a task, because
        ambient mode has no task object to carry them.
        """
        try:
            captured = AmbientFrameContext.model_validate(captured_value)
        except ValueError as exc:
            self.session_trace(
                "ambient.observation.rejected",
                "ambient-policy",
                {"reason": "invalid-captured-context", "error": str(exc)[:240]},
            )
            return {"accepted": False, "reason": "invalid-captured-context"}

        def reject(reason: str, **detail: Any) -> dict[str, Any]:
            if self.owner.ambient:
                self.owner.ambient.record_rejection(
                    reason,
                    frame_id=captured.frame_id,
                    context_id=captured.context_id,
                    detail=detail,
                )
            self.session_trace(
                "ambient.observation.rejected",
                "ambient-policy",
                {
                    "reason": reason,
                    "frameId": captured.frame_id,
                    "contextId": captured.context_id,
                    "watchlistId": captured.watchlist_id,
                    **detail,
                },
            )
            self.journal.append(
                "ambient_observation",
                {
                    "status": "rejected",
                    "reason": reason,
                    "frameId": captured.frame_id,
                    "contextId": captured.context_id,
                    "watchlistId": captured.watchlist_id,
                    "rawMediaPersisted": bool(captured.media),
                    "media": captured.media,
                },
            )
            return {"accepted": False, "reason": reason}

        async with self.owner.mutation_lock:
            if not raw_value.get("valid"):
                return reject(
                    "malformed-provider-output",
                    error=str(raw_value.get("error", "invalid output"))[:240],
                )
            watch = self.ambient_watch_session(self.owner)
            if watch is None:
                return reject("ambient-not-authorized")
            current = watch.frame_context(
                user_id=self.user_id,
                session_id=self.session_id,
                frame_id=captured.frame_id,
                captured_at=captured.frame_captured_at,
            )
            try:
                hypothesis = AmbientObservationHypothesis.model_validate(
                    {
                        key: value
                        for key, value in raw_value.items()
                        if key not in {"valid", "error"}
                    }
                )
                validation = validate_ambient_result(
                    captured, current, hypothesis, source=f"camera/{self.model}"
                )
            except ValueError as exc:
                return reject("invalid-provider-schema", error=str(exc)[:240])
            if not validation.accepted or validation.observation is None:
                return reject(validation.reason)
            evidence = validation.observation
            fresh_for_speech = (
                evidence.frame_age_ms <= perception_settings()["liveMaxAgeMs"]
            )
            watch.ingest(dict(evidence.evidence))
            self.product_database.record_observation(evidence)
            observation_entry = self.journal.append(
                "ambient_observation",
                {
                    **evidence.model_dump(mode="json"),
                    "status": "accepted",
                    "freshForSpeech": fresh_for_speech,
                    "rawMediaPersisted": bool(captured.media),
                    "referenceContext": captured.reference_context,
                },
            )
            self.owner.scene.ingest_observation(observation_entry)
            self.owner.scene.events.schedule()
            policy = self.current_memory_policy()
            if (
                (
                    hypothesis.observed
                    if captured.observation_mode == "focused"
                    else bool(hypothesis.visible_evidence)
                )
                and (captured.previous_observation or {}).get("visibleEvidence")
                != hypothesis.visible_evidence
                and policy["components"]["timeline"]["episodic_enabled"]
            ):
                card = self.current_episodic_memory(policy).remember(
                    "；".join(hypothesis.visible_evidence),
                    kind="observation",
                    source=evidence.source,
                    skill=None,
                    task=evidence.task_id,
                    session_id=self.session_id,
                    user_scope=self.user_id,
                    evidence_event_id=observation_entry["id"],
                    happened_at=captured.frame_captured_at.isoformat(),
                    recorded_at=evidence.received_at.isoformat(),
                    timezone_name=self.clock_for_session(
                        self.user_id, self.session_id
                    ).timezone_name,
                    utc_offset_minutes=self.clock_for_session(
                        self.user_id, self.session_id
                    ).utc_offset_minutes,
                    evidence_context={
                        "frame": captured.media,
                        "observationId": evidence.observation_id,
                        "uncertainties": hypothesis.uncertainties,
                        "claim": "visual-observation",
                    },
                )
                if card:
                    self.session_trace(
                        "timeline.memory.written",
                        "ambient-observer",
                        {
                            "cardId": card["id"],
                            "observationId": evidence.observation_id,
                            "frameId": captured.frame_id,
                            "happenedAt": captured.frame_captured_at.isoformat(),
                        },
                    )
            speech = None
            pending = self.timeline.pending_draft(
                user_id=self.user_id, case_id=self.session_id
            )
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
            )
            self.owner.scene.ingest_review(review)
            if review["status"] == "ready":
                speech = {
                    "utterance": prompt_text(
                        "perception/ambient_question", question=hypothesis.clarification
                    ),
                    "reason": "clarification",
                }
            snapshot = watch.snapshot()
        state_payload = self.scoped_state_for_user(self.user_id, self.session_id)
        if not self.ws.closed:
            await self.ws.send_json({"type": "state", "state": state_payload})
            await self.ws.send_json({"type": "ambient", "ambient": snapshot})
        return {
            "accepted": True,
            "reason": validation.reason,
            "speech": speech,
            "origin": {
                "observationReviewId": review["candidateId"],
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

    async def session_perception_result(
        self, captured_value: dict[str, Any], raw_value: dict[str, Any]
    ) -> dict[str, Any]:
        if captured_value.get("schema_version") == AMBIENT_SCHEMA_VERSION:
            return await self.session_ambient_result(captured_value, raw_value)
        try:
            captured = PerceptionFrameContext.model_validate(captured_value)
        except ValueError as exc:
            self.session_trace(
                "perception.observation.rejected",
                "perception-policy",
                {"reason": "invalid-captured-context", "error": str(exc)[:240]},
            )
            return {"accepted": False, "reason": "invalid-captured-context"}
        async with self.owner.mutation_lock:
            active_task = self.session_runtime.task
            if active_task is None:
                self.session_trace(
                    "perception.observation.rejected",
                    "perception-policy",
                    {
                        "reason": "no-active-task",
                        "frameId": captured.frame_id,
                        "contextId": captured.context_id,
                    },
                )
                return {"accepted": False, "reason": "no-active-task"}
            if not raw_value.get("valid"):
                active_task.record_perception_rejection(
                    "malformed-provider-output",
                    frame_id=captured.frame_id,
                    context_id=captured.context_id,
                    detail={
                        "error": str(raw_value.get("error", "invalid output"))[:240]
                    },
                )
                return {"accepted": False, "reason": "malformed-provider-output"}
            current = self.perception_context_for_runtime(
                self.session_runtime,
                user_id=self.user_id,
                session_id=self.session_id,
                frame_id=captured.frame_id,
                captured_at=captured.frame_captured_at,
            )
            if current is None:
                active_task.record_perception_rejection(
                    "no-active-task-context",
                    frame_id=captured.frame_id,
                    context_id=captured.context_id,
                )
                return {"accepted": False, "reason": "no-active-task-context"}
            try:
                hypothesis = PerceptionHypothesis.model_validate(
                    {
                        key: value
                        for key, value in raw_value.items()
                        if key not in {"valid", "error"}
                    }
                )
                validation = validate_perception_result(
                    captured, current, hypothesis, source=f"camera/{self.model}"
                )
            except ValueError as exc:
                active_task.record_perception_rejection(
                    "invalid-provider-schema",
                    frame_id=captured.frame_id,
                    context_id=captured.context_id,
                    detail={"error": str(exc)[:240]},
                )
                return {"accepted": False, "reason": "invalid-provider-schema"}
            if not validation.accepted or validation.observation is None:
                active_task.record_perception_rejection(
                    validation.reason,
                    frame_id=captured.frame_id,
                    context_id=captured.context_id,
                    detail={
                        "capturedCheckpoint": captured.checkpoint_id,
                        "currentCheckpoint": current.checkpoint_id,
                        "hypothesisCheckpoint": hypothesis.checkpoint_id,
                    },
                )
                self.journal.append_state_checkpoint(
                    self.runtime_checkpoint(self.session_runtime),
                    reason="perception-evidence-rejected",
                )
                state_payload = self.scoped_state_for_user(
                    self.user_id, self.session_id
                )
                if not self.ws.closed:
                    await self.ws.send_json({"type": "state", "state": state_payload})
                return {"accepted": False, "reason": validation.reason}
            evidence = validation.observation
            active_task.ingest(evidence.model_dump(mode="json"))
            self.product_database.record_observation(evidence)
            self.journal.append(
                "perception_observation",
                {
                    **evidence.model_dump(mode="json"),
                    "status": "accepted",
                    "rawMediaPersisted": False,
                },
            )
            policy = self.current_memory_policy()
            episode = None
            if policy["components"]["timeline"]["episodic_enabled"]:
                episode = self.current_episodic_memory(policy).remember(
                    str(evidence.evidence.get("summary", "")),
                    kind="observation",
                    source=evidence.source,
                    skill=self.session_runtime.skill.name
                    if self.session_runtime.skill
                    else None,
                    task=evidence.task_id,
                    session_id=self.session_id,
                    user_scope=self.user_id,
                )
            self.apply_memory_hint(
                self.session_runtime,
                episodic_memory=self.current_episodic_memory(policy),
                enabled=policy["components"]["timeline"]["preference_enabled"],
            )
            self.journal.append_state_checkpoint(
                self.runtime_checkpoint(self.session_runtime),
                reason="task-bound-perception-evidence",
            )
            task_snapshot = active_task.snapshot()
            state_payload = self.scoped_state_for_user(self.user_id, self.session_id)
            if not self.ws.closed:
                await self.ws.send_json({"type": "state", "state": state_payload})
            if (
                episode
                and episode.get("status") == "active"
                and (episode.get("tier") == "short")
            ):
                self.request_timeline_consolidation(self.owner)
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
            "reason": validation.reason,
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
