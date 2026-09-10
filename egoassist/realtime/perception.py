"""Checkpoint-bound perception contracts, policy, and optional HTTP adapter."""

from __future__ import annotations

from egoassist.prompts import prompt_text

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from egoassist.product.contracts import (
    AmbientFrameContext,
    AmbientObservationHypothesis,
    ObservationEvidence,
    PerceptionFrameContext,
    PerceptionHypothesis,
)
from egoassist.realtime.ambient import (
    AMBIENT_SCHEMA_VERSION,
    NONE_OBSERVED,
    UNKNOWN_LOCATION,
)


MAX_FRAME_AGE_MS = 5_000


@dataclass(frozen=True)
class PerceptionValidation:
    accepted: bool
    reason: str
    observation: ObservationEvidence | None = None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _frame_age_ms(captured_at: datetime, received: datetime) -> int:
    return int((received - _utc(captured_at)).total_seconds() * 1000)


def _binding_mismatch(captured: Any, current: Any, fields: tuple[str, ...]) -> bool:
    return any(getattr(captured, key) != getattr(current, key) for key in fields)


def perception_prompt(context: PerceptionFrameContext | Mapping[str, Any]) -> str:
    value = (
        context
        if isinstance(context, PerceptionFrameContext)
        else PerceptionFrameContext.model_validate(context)
    )
    candidates = [value.checkpoint_id, *value.allowed_transitions, "unknown"]
    artifacts = [
        {
            "artifactId": item.get("artifact_id"),
            "checksum": item.get("checksum"),
            "stepId": item.get("step_id"),
        }
        for item in value.authorized_artifacts
    ]
    return (
        prompt_text('perception/perception_prompt.1')
        + json.dumps(
            {
                "schema": value.schema_version,
                "taskId": value.task_id,
                "taskVersion": value.task_version,
                "taskProvenance": value.task_provenance,
                "currentCheckpoint": value.checkpoint_id,
                "checkpointEpoch": value.checkpoint_epoch,
                "checkpointCandidates": list(dict.fromkeys(candidates)),
                "completionCriteria": value.completion_criteria,
                "risk": value.risk,
                "sourceReference": value.source_reference,
                "authorizedArtifacts": artifacts,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _parse_json_object(raw):
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if lines[0].strip().lower() not in {"```", "```json"}:
                raise ValueError("unsupported perception output fence")
            text = "\n".join(lines[1:-1])
        value = json.loads(text)
    else:
        value = dict(raw)
    if not isinstance(value, dict):
        raise ValueError("perception output must be a JSON object")
    return value


def parse_perception_hypothesis(raw: str | Mapping[str, Any]) -> PerceptionHypothesis:
    value = _parse_json_object(raw)
    hazards = []
    for item in value.get("hazards", []):
        if not isinstance(item, Mapping):
            raise ValueError("perception hazards must be objects")
        hazards.append(
            {
                "kind": str(item.get("kind") or item.get("type") or "task-safety")[:80],
                "message": " ".join(str(item.get("message", "")).split())[:300],
                "confidence": item.get("confidence", 0),
            }
        )
    visible = value.get("visible_evidence", value.get("visibleEvidence", []))
    if not isinstance(visible, list):
        raise ValueError("visible_evidence must be a list")
    return PerceptionHypothesis(
        checkpoint_id=str(value.get("checkpoint_id") or value.get("step") or "unknown"),
        completed=value.get("completed", False),
        confidence=value.get("confidence", 0),
        summary=" ".join(str(value.get("summary") or value.get("scene_summary") or "").split())[:500],
        visible_evidence=[" ".join(str(item).split())[:300] for item in visible[:8] if str(item).strip()],
        hazards=hazards,
        unrelated=value.get("unrelated", False),
    )


def validate_perception_result(
    context: PerceptionFrameContext | Mapping[str, Any],
    current_context: PerceptionFrameContext | Mapping[str, Any],
    hypothesis: PerceptionHypothesis | Mapping[str, Any],
    *,
    received_at: datetime | None = None,
    max_frame_age_ms: int = MAX_FRAME_AGE_MS,
    source: str,
) -> PerceptionValidation:
    captured = (
        context
        if isinstance(context, PerceptionFrameContext)
        else PerceptionFrameContext.model_validate(context)
    )
    current = (
        current_context
        if isinstance(current_context, PerceptionFrameContext)
        else PerceptionFrameContext.model_validate(current_context)
    )
    output = (
        hypothesis
        if isinstance(hypothesis, PerceptionHypothesis)
        else PerceptionHypothesis.model_validate(hypothesis)
    )
    received = _utc(received_at or datetime.now(timezone.utc))
    age_ms = int((received - _utc(captured.frame_captured_at)).total_seconds() * 1000)
    if age_ms < 0:
        return PerceptionValidation(False, "frame-captured-in-future")
    if age_ms > max_frame_age_ms:
        return PerceptionValidation(False, "stale-frame")
    binding_fields = (
        "context_id",
        "user_id",
        "session_id",
        "skill_id",
        "task_id",
        "task_version",
        "checkpoint_id",
        "checkpoint_epoch",
    )
    if any(getattr(captured, key) != getattr(current, key) for key in binding_fields):
        return PerceptionValidation(False, "task-context-changed")
    if captured.allowed_transitions != current.allowed_transitions:
        return PerceptionValidation(False, "allowed-path-changed")
    allowed = {captured.checkpoint_id, *captured.allowed_transitions, "unknown"}
    if output.checkpoint_id not in allowed:
        return PerceptionValidation(False, "checkpoint-outside-allowed-path")
    if output.unrelated:
        return PerceptionValidation(False, "unrelated-frame")
    if output.completed and output.checkpoint_id != captured.checkpoint_id:
        return PerceptionValidation(False, "completion-not-current-checkpoint")
    observation = ObservationEvidence(
        observation_id=f"observation-{uuid4().hex}",
        user_id=captured.user_id,
        session_id=captured.session_id,
        task_id=captured.task_id,
        checkpoint_id=output.checkpoint_id,
        frame_captured_at=captured.frame_captured_at,
        received_at=received,
        frame_age_ms=age_ms,
        allowed_transitions=list(captured.allowed_transitions),
        confidence=output.confidence,
        hazards=output.hazards,
        source=source,
        evidence={
            "schemaVersion": captured.schema_version,
            "contextId": captured.context_id,
            "frameId": captured.frame_id,
            "skillId": captured.skill_id,
            "taskVersion": captured.task_version,
            "taskProvenance": captured.task_provenance,
            "boundCheckpointId": captured.checkpoint_id,
            "checkpointEpoch": captured.checkpoint_epoch,
            "completionCriteria": captured.completion_criteria,
            "completionHypothesis": output.completed,
            "summary": output.summary,
            "visibleEvidence": output.visible_evidence,
            "sourceReference": captured.source_reference,
            "artifactChecksums": [
                item.get("checksum") for item in captured.authorized_artifacts if item.get("checksum")
            ],
        },
    )
    return PerceptionValidation(True, "accepted-task-bound-evidence", observation)


def ambient_prompt(context: AmbientFrameContext | Mapping[str, Any]) -> str:
    value = (
        context
        if isinstance(context, AmbientFrameContext)
        else AmbientFrameContext.model_validate(context)
    )
    activities = [
        {
            "activityId": item.get("id"),
            "label": item.get("label"),
            "visibleEvidence": item.get("visibleEvidence"),
        }
        for item in value.watch_activities
    ]
    locations = [
        {"locationId": item.get("id"), "label": item.get("label")}
        for item in value.watch_locations
    ]
    return (
        (prompt_text('perception/ambient_open') if value.observation_mode == "open"
         else prompt_text('perception/ambient_prompt.1', v0=NONE_OBSERVED, v1=UNKNOWN_LOCATION))
        + prompt_text('perception/reference_context')
        + json.dumps(
            {
                "schema": value.schema_version,
                "watchlistId": value.watchlist_id,
                "watchlistVersion": value.watchlist_version,
                "watchlistProvenance": value.watchlist_provenance,
                "watchEpoch": value.watch_epoch,
                **({"activityCandidates": activities, "locationCandidates": locations} if value.observation_mode == "focused" else {}),
                "noneObservedId": NONE_OBSERVED,
                "unknownLocationId": UNKNOWN_LOCATION,
                "risk": value.risk,
                "capturedAt": value.frame_captured_at.isoformat(),
                "previousObservation": value.previous_observation,
                "referenceContext": value.reference_context,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def frame_prompt(
    context: PerceptionFrameContext | AmbientFrameContext | Mapping[str, Any],
) -> str:
    """Dispatch to the task or ambient prompt by schema version."""
    if isinstance(context, AmbientFrameContext):
        return ambient_prompt(context)
    if isinstance(context, PerceptionFrameContext):
        return perception_prompt(context)
    version = str(dict(context).get("schema_version", ""))
    return ambient_prompt(context) if version == AMBIENT_SCHEMA_VERSION else perception_prompt(context)


def parse_ambient_hypothesis(raw: str | Mapping[str, Any], *, open_observation=False) -> AmbientObservationHypothesis:
    value = _parse_json_object(raw)
    if open_observation:
        if not {"summary", "visible_evidence", "uncertainties", "salient_change"} <= value.keys():
            raise ValueError("open observation is missing evidence fields")
        value = {**value, "observed": bool(value["visible_evidence"]),
                 "activity_id": str(value["summary"])[:160] if value["visible_evidence"] else NONE_OBSERVED}
    elif not ({"activity_id", "observed", "summary"} <= value.keys() or
            {"activityId", "observed", "summary"} <= value.keys()):
        raise ValueError("ambient output is missing required observation fields")
    hazards = []
    for item in value.get("hazards", []):
        if not isinstance(item, Mapping):
            raise ValueError("ambient hazards must be objects")
        hazards.append(
            {
                "kind": str(item.get("kind") or item.get("type") or "ambient-safety")[:80],
                "message": " ".join(str(item.get("message", "")).split())[:300],
                "confidence": item.get("confidence", 0),
            }
        )
    visible = value.get("visible_evidence", value.get("visibleEvidence", []))
    if not isinstance(visible, list):
        raise ValueError("visible_evidence must be a list")
    return AmbientObservationHypothesis(
        activity_id=str(value.get("activity_id") or value.get("activityId") or NONE_OBSERVED),
        observed=value.get("observed", False),
        location_id=str(value.get("location_id") or value.get("locationId") or UNKNOWN_LOCATION),
        confidence=value.get("confidence", 0),
        summary=" ".join(str(value.get("summary") or value.get("scene_summary") or "").split())[:500],
        visible_evidence=[" ".join(str(item).split())[:300] for item in visible[:8] if str(item).strip()],
        hazards=hazards,
        uncertainties=value.get("uncertainties", []),
        clarification=value.get("clarification") or None,
        salient_change=value.get("salient_change", True),
    )


def validate_ambient_result(
    context: AmbientFrameContext | Mapping[str, Any],
    current_context: AmbientFrameContext | Mapping[str, Any],
    hypothesis: AmbientObservationHypothesis | Mapping[str, Any],
    *,
    received_at: datetime | None = None,
    max_frame_age_ms: int | None = None,
    source: str,
) -> PerceptionValidation:
    """Validate a task-free ambient observation.

    A separate function from ``validate_perception_result`` by design: ambient
    code paths are structurally incapable of weakening the task-mode checks,
    which is the verification claim ADR 007 rests on.

    Freshness and binding immutability are preserved unchanged. What differs is
    that absence is accepted as evidence, and that there is no completion
    concept at all.
    """
    captured = (
        context
        if isinstance(context, AmbientFrameContext)
        else AmbientFrameContext.model_validate(context)
    )
    current = (
        current_context
        if isinstance(current_context, AmbientFrameContext)
        else AmbientFrameContext.model_validate(current_context)
    )
    output = (
        hypothesis
        if isinstance(hypothesis, AmbientObservationHypothesis)
        else AmbientObservationHypothesis.model_validate(hypothesis)
    )
    received = _utc(received_at or datetime.now(timezone.utc))
    age_ms = _frame_age_ms(captured.frame_captured_at, received)
    if age_ms < 0:
        return PerceptionValidation(False, "frame-captured-in-future")
    if max_frame_age_ms is not None and age_ms > max_frame_age_ms:
        return PerceptionValidation(False, "stale-frame")
    binding_fields = (
        "context_id",
        "user_id",
        "session_id",
        "watchlist_id",
        "watchlist_version",
        "watchlist_checksum",
        "watch_epoch",
        "observation_mode",
        # Consent is part of the binding, so revoking it invalidates every
        # frame already in flight rather than only future ones.
        "consent_id",
    )
    if _binding_mismatch(captured, current, binding_fields):
        return PerceptionValidation(False, "ambient-context-changed")
    if captured.watch_activities != current.watch_activities:
        return PerceptionValidation(False, "watchlist-changed")
    if captured.watch_locations != current.watch_locations:
        return PerceptionValidation(False, "watchlist-changed")
    allowed_activities = {
        str(item.get("id")) for item in captured.watch_activities
    } | {NONE_OBSERVED}
    if captured.observation_mode == "focused" and output.activity_id not in allowed_activities:
        return PerceptionValidation(False, "activity-outside-watchlist")
    allowed_locations = {
        str(item.get("id")) for item in captured.watch_locations
    } | {UNKNOWN_LOCATION}
    if captured.observation_mode == "focused" and output.location_id not in allowed_locations:
        return PerceptionValidation(False, "location-outside-watchlist")
    if output.observed and output.activity_id == NONE_OBSERVED:
        return PerceptionValidation(False, "contradictory-absence-claim")
    if not output.observed and output.activity_id != NONE_OBSERVED:
        return PerceptionValidation(False, "contradictory-absence-claim")
    if output.observed and not output.visible_evidence:
        return PerceptionValidation(False, "positive-observation-without-evidence")
    observation = ObservationEvidence(
        observation_id=f"observation-{uuid4().hex}",
        user_id=captured.user_id,
        session_id=captured.session_id,
        # Namespaced so ambient rows are provably disjoint from task rows:
        # TaskSpec ids cannot contain a colon.
        task_id=f"ambient:{captured.watchlist_id}",
        checkpoint_id=output.activity_id,
        frame_captured_at=captured.frame_captured_at,
        received_at=received,
        frame_age_ms=age_ms,
        allowed_transitions=[],
        confidence=output.confidence,
        hazards=output.hazards,
        source=source,
        evidence={
            "schemaVersion": captured.schema_version,
            "contextId": captured.context_id,
            "frameId": captured.frame_id,
            "watchlistId": captured.watchlist_id,
            "watchlistVersion": captured.watchlist_version,
            "watchlistProvenance": captured.watchlist_provenance,
            "watchlistChecksum": captured.watchlist_checksum,
            "watchEpoch": captured.watch_epoch,
            "consentId": captured.consent_id,
            "observed": output.observed,
            "activityId": output.activity_id,
            "locationId": output.location_id,
            "summary": output.summary,
            "visibleEvidence": output.visible_evidence,
            "capturedAt": captured.frame_captured_at.isoformat(),
            "media": captured.media,
            "observationMode": captured.observation_mode,
            "uncertainties": output.uncertainties,
            "clarification": output.clarification,
            "salientChange": output.salient_change,
        },
    )
    return PerceptionValidation(True, "accepted-ambient-evidence", observation)


class StructuredPerceptionAdapter:
    """Optional OpenAI-compatible adapter returning conservative hypotheses."""

    def __init__(self, mode: str = "manual", model: str | None = None, endpoint: str | None = None, api_key: str | None = None) -> None:
        self.mode = mode
        self.model = model or os.getenv("EGOASSIST_PERCEPTION_MODEL", "gpt-4o-mini")
        self.endpoint = endpoint or os.getenv("EGOASSIST_PERCEPTION_ENDPOINT", "https://api.openai.com/v1/chat/completions")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        if self.mode == "openai" and not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for openai perception mode")

    def _http_post(self, request: urllib.request.Request) -> dict[str, Any]:
        """Synchronous HTTP POST — wrap in a worker thread when calling from async code."""
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def analyze(self, image_data_url: str, current_checkpoint: str, allowed_checkpoints: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        if self.mode != "openai":
            return {
                "step": "unknown", "completed": False, "confidence": 0.0,
                "summary": "Camera connected; manual mode makes no model claim about the frame.",
                "hazards": [], "visible_evidence": [], "unrelated": True,
                "source": "camera/manual",
            }
        if not re.match(r"^data:image/(jpeg|png|webp);base64,", image_data_url):
            raise ValueError("frame must be a base64 image data URL")
        if len(image_data_url) > 2_000_000:
            raise ValueError("frame is too large")
        allowed = list(dict.fromkeys([current_checkpoint, *allowed_checkpoints, "unknown"]))
        if allowed == ["unknown"]:
            raise ValueError("allowed checkpoints are required for model perception")
        prompt = (
            prompt_text('perception/analyze.5', v0=json.dumps(allowed))
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 350,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url, "detail": "low"}},
            ]}],
        }
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        result = self._http_post(req)
        observation = parse_perception_hypothesis(
            json.loads(result["choices"][0]["message"]["content"])
        ).model_dump(mode="json")
        observation["step"] = observation.pop("checkpoint_id")
        observation["source"] = f"camera/{self.model}"
        return observation
