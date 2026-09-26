from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from egoassist.core.settings import observation_speech_max_age_ms
from egoassist.product import (
    AmbientFrameContext,
    AmbientObservationHypothesis,
    PerceptionFrameContext,
    PerceptionHypothesis,
)
from egoassist.core.lane_contracts import AcceptedObservation
from egoassist.realtime import validate_ambient_result, validate_perception_result
from egoassist.realtime.ambient import AMBIENT_SCHEMA_VERSION


@dataclass
class ObservationService:
    ambient_watch_session: Any
    bridge: Any
    journal: Any
    model: Any
    owner: Any
    perception_context_for_runtime: Any
    product_database: Any
    session_id: Any
    session_runtime: Any
    session_trace: Any
    memory_service: Any
    interaction: Any
    user_id: Any

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
            self.memory_service.timeline,
            task=self.session_runtime.task.snapshot()
            if self.session_runtime.task
            else None,
        )
        options = self.interaction.ambient_options(self.owner)
        context["reference_context"]["outputPolicy"] = {
            "remindersEnabled": options.get("remindersEnabled", True),
            "questionsEnabled": options["questionsEnabled"],
        }
        permission = self.interaction.interaction_state.snapshot()
        context["reference_context"]["speechPermission"] = {
            key: permission.get(key) for key in (
                "quiet", "until", "paused", "permissionSource", "proactiveAllowed")
        }
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
                evidence.frame_age_ms <= observation_speech_max_age_ms(hypothesis.notification)
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
            result = self.interaction.ambient(AcceptedObservation(captured, hypothesis, evidence,
                observation_entry, validation.reason))
            return {**result, 'ambient': watch.snapshot()}

    async def session_perception_result(self, captured_value: dict[str, Any], raw_value: dict[str, Any]) -> dict[str, Any]:
        if captured_value.get('schema_version') == AMBIENT_SCHEMA_VERSION:
            return await self.session_ambient_result(captured_value, raw_value)
        try:
            captured = PerceptionFrameContext.model_validate(captured_value)
        except ValueError as exc:
            self.session_trace('perception.observation.rejected', 'perception-policy',
                {'reason': 'invalid-captured-context', 'error': str(exc)[:240]})
            return {'accepted': False, 'reason': 'invalid-captured-context'}
        async with self.owner.mutation_lock:
            if self.session_runtime.task is None:
                return {'accepted': False, 'reason': 'no-active-task'}
            if not raw_value.get('valid'):
                return self.interaction.reject_task('malformed-provider-output', captured,
                    error=str(raw_value.get('error', 'invalid output'))[:240])
            current = self.perception_context_for_runtime(self.session_runtime,
                user_id=self.user_id, session_id=self.session_id,
                frame_id=captured.frame_id, captured_at=captured.frame_captured_at)
            if current is None:
                return self.interaction.reject_task('no-active-task-context', captured)
            try:
                hypothesis = PerceptionHypothesis.model_validate({key: value for key, value in raw_value.items()
                    if key not in {'valid', 'error'}})
                validation = validate_perception_result(captured, current, hypothesis, source=f'camera/{self.model}')
            except ValueError as exc:
                return self.interaction.reject_task('invalid-provider-schema', captured, error=str(exc)[:240])
            if not validation.accepted or validation.observation is None:
                return self.interaction.reject_task(validation.reason, captured,
                    capturedCheckpoint=captured.checkpoint_id, currentCheckpoint=current.checkpoint_id,
                    hypothesisCheckpoint=hypothesis.checkpoint_id)
            evidence = validation.observation
            self.product_database.record_observation(evidence)
            entry = self.journal.append('perception_observation', {**evidence.model_dump(mode='json'),
                'status': 'accepted', 'rawMediaPersisted': False})
            return self.interaction.task(AcceptedObservation(captured, hypothesis, evidence, entry, validation.reason))
