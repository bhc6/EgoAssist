"""Checkpoint-bound perception contracts, policy, and optional HTTP adapter."""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from egoassist.product.contracts import (
    ObservationEvidence,
    PerceptionFrameContext,
    PerceptionHypothesis,
)


MAX_FRAME_AGE_MS = 5_000


@dataclass(frozen=True)
class PerceptionValidation:
    accepted: bool
    reason: str
    observation: ObservationEvidence | None = None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


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
        "Inspect only the submitted egocentric frame against the immutable task context below. "
        "Return one JSON object with checkpoint_id, completed, confidence, summary, "
        "visible_evidence, hazards, and unrelated. hazards is an array of "
        "{kind,message,confidence}. checkpoint_id must be one of the supplied candidates. "
        "completed is only a visual hypothesis, never a command to advance the task. "
        "Do not infer invisible actions, identity, intent, or facts from reference text alone. "
        "Use unrelated=true when the frame does not show evidence relevant to this checkpoint. "
        "Use Chinese for summary and hazard messages.\n"
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


def parse_perception_hypothesis(raw: str | Mapping[str, Any]) -> PerceptionHypothesis:
    value = json.loads(raw.strip()) if isinstance(raw, str) else dict(raw)
    if not isinstance(value, dict):
        raise ValueError("perception output must be a JSON object")
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
        completed=bool(value.get("completed", False)),
        confidence=value.get("confidence", 0),
        summary=" ".join(str(value.get("summary") or value.get("scene_summary") or "").split())[:500],
        visible_evidence=[" ".join(str(item).split())[:300] for item in visible[:8] if str(item).strip()],
        hazards=hazards,
        unrelated=bool(value.get("unrelated", False)),
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
        """Synchronous HTTP POST — call via asyncio.to_thread() from async contexts."""
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
            "Conservatively map visible egocentric evidence to a checkpoint hypothesis. "
            f"Return JSON only. checkpoint_id must be one of {json.dumps(allowed)}; include "
            "completed, confidence, summary, visible_evidence, hazards, and unrelated. "
            "Completion is evidence only, never a task advancement command."
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

    async def analyze_async(self, image_data_url: str, current_checkpoint: str, allowed_checkpoints: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        """Async entry-point — runs the blocking HTTP call off the event loop.

        Use this from any async context (agent loop, LiveKit handler, etc.) to
        avoid stalling the event loop for up to the 30 s HTTP timeout.
        """
        return await asyncio.to_thread(self.analyze, image_data_url, current_checkpoint, allowed_checkpoints)
