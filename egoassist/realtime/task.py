"""Generic task specification and risk-controlled guidance state machine."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


def bounded_text(value: Any, field: str, maximum: int) -> str:
    """Validate a required, stripped, length-bounded string field.

    Shared with the ambient watchlist loader so both specification formats get
    identical bounds-checking instead of a divergent copy.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} cannot be empty")
    if len(text) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return text


@dataclass(frozen=True)
class TaskStep:
    id: str
    title: str
    instruction: str
    completion: str
    risk: str = "low"
    source: str = ""


@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    version: str
    role: str
    safety_policy: tuple[str, ...]
    steps: tuple[TaskStep, ...]
    aliases: tuple[str, ...] = ()
    provenance: str = "user"

    @classmethod
    def load(cls, path: str | Path) -> "TaskSpec":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(payload, provenance=f"file:{Path(path).name}")

    @classmethod
    def from_dict(cls, payload: dict[str, Any], provenance: str = "api") -> "TaskSpec":
        required = {"id", "title", "version", "role", "steps"}
        if not isinstance(payload, dict) or not required.issubset(payload):
            raise ValueError("invalid task specification")
        task_id = cls._bounded_text(payload["id"], "task id", 64)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", task_id):
            raise ValueError("task id must use 2-64 lowercase letters, digits, hyphens or underscores")
        title = cls._bounded_text(payload["title"], "task title", 160)
        version = cls._bounded_text(payload["version"], "task version", 64)
        role = cls._bounded_text(payload["role"], "task role", 600)
        if not isinstance(payload["steps"], list) or not payload["steps"] or len(payload["steps"]) > 16:
            raise ValueError("task requires 1-16 steps")
        steps: list[TaskStep] = []
        seen: set[str] = set()
        for raw in payload["steps"]:
            if not isinstance(raw, dict):
                raise ValueError("each task step must be an object")
            unknown = set(raw) - {"id", "title", "instruction", "completion", "risk", "source"}
            if unknown:
                raise ValueError(f"unknown task step field(s): {', '.join(sorted(unknown))}")
            missing = {"id", "title", "instruction", "completion"} - set(raw)
            if missing:
                raise ValueError(f"task step missing: {', '.join(sorted(missing))}")
            step_id = cls._bounded_text(raw["id"], "task step id", 64)
            if step_id in seen or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", step_id):
                raise ValueError(f"invalid or duplicate step id: {step_id}")
            risk = cls._bounded_text(raw.get("risk", "low"), "task step risk", 16)
            if risk not in {"low", "medium", "high"}:
                raise ValueError(f"invalid risk level: {risk}")
            source = raw.get("source", "")
            if not isinstance(source, str) or len(source) > 300:
                raise ValueError("task step source must be a string of at most 300 characters")
            step = TaskStep(
                id=step_id,
                title=cls._bounded_text(raw["title"], "task step title", 160),
                instruction=cls._bounded_text(raw["instruction"], "task step instruction", 1200),
                completion=cls._bounded_text(raw["completion"], "task step completion", 600),
                risk=risk,
                source=source.strip(),
            )
            seen.add(step_id)
            steps.append(step)
        safety_policy = payload.get("safety_policy", [])
        if not isinstance(safety_policy, list) or len(safety_policy) > 16:
            raise ValueError("task safety_policy must contain at most 16 strings")
        aliases = payload.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 32:
            raise ValueError("task aliases must contain at most 32 strings")
        resolved_provenance = payload.get("provenance", provenance)
        return cls(
            id=task_id,
            title=title,
            version=version,
            role=role,
            safety_policy=tuple(
                cls._bounded_text(item, "task safety policy item", 500)
                for item in safety_policy
            ),
            steps=tuple(steps),
            aliases=tuple(cls._bounded_text(item, "task alias", 120) for item in aliases),
            provenance=cls._bounded_text(resolved_provenance, "task provenance", 200),
        )

    @staticmethod
    def _bounded_text(value: Any, field: str, maximum: int) -> str:
        return bounded_text(value, field, maximum)

    def realtime_instructions(self, memories: list[dict[str, Any]]) -> str:
        steps = "\n".join(
            f"{index + 1}. [{step.id}] {step.title}: {step.instruction}; 完成证据: {step.completion}; 风险: {step.risk}; 来源: {step.source}"
            for index, step in enumerate(self.steps)
        )
        memory = "\n".join(
            f"- 触发 {item.get('trigger')}: {item.get('prompt')}（用户已确认）"
            for item in memories
        ) or "- 暂无用户确认的长期偏好"
        safety = "\n".join(f"- {item}" for item in self.safety_policy)
        return (
            f"你是{self.role}。当前任务是《{self.title}》，流程版本 {self.version}，来源 {self.provenance}。\n"
            "你会持续收到第一视角视频和用户语音。自然交流，可被用户随时打断。"
            "只依据当前可见证据、任务规范和已确认记忆；看不清就询问，不猜测。"
            "任务步骤是可解释的目标与前置证据地图，不是必须逐字播放的说明书。现场路径偏离时先判断安全和依赖关系，"
            "允许回退、澄清、恢复或用户确认后的顺序调整，不得为追赶步骤而忽略现场证据。"
            "不要逐帧播报，也不要每步都打断；仅在用户请求、明显风险、需要确认的偏离或下一条短提示确有帮助时说话。"
            "每次最多给一个动作。看到可能完成时先请用户确认，只有用户明确确认后结构化状态才推进。\n"
            f"任务步骤：\n{steps}\n安全策略：\n{safety}\n长期个人记忆：\n{memory}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "version": self.version,
            "role": self.role,
            "safety_policy": list(self.safety_policy),
            "steps": [asdict(step) for step in self.steps],
            "aliases": list(self.aliases),
            "provenance": self.provenance,
        }


class TaskSession:
    def __init__(
        self,
        spec: TaskSpec,
        event_sink: Callable[[str, str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.spec = spec
        self._event_sink = event_sink
        self.reset()

    def set_event_sink(self, event_sink: Callable[..., Any] | None) -> None:
        self._event_sink = event_sink

    def reset(self) -> dict[str, Any]:
        self.perception_context_id = f"task-context-{uuid4().hex}"
        self.checkpoint_epoch = 0
        self.completion_evidence: list[dict[str, Any]] = []
        self.perception_audit: list[dict[str, Any]] = []
        self.step_index = 0
        self.current_checkpoint_id = self.spec.steps[0].id
        self.checkpoints = {
            step.id: {
                "status": "active" if index == 0 else "unknown",
                "confidence": 1.0 if index == 0 else 0.0,
                "evidenceIds": [],
                "updatedBy": "session-start" if index == 0 else None,
            }
            for index, step in enumerate(self.spec.steps)
        }
        self.completed = False
        self.paused = False
        self.observation: dict[str, Any] | None = None
        self.announced_memory_ids: set[str] = set()
        self.decision = self._decision("guide", self.spec.steps[0].instruction, "开始任务", 1.0)
        self.trace: list[dict[str, Any]] = []
        self._emit("task.session.started", {"task": self.spec.id, "checkpoint": self.current_checkpoint_id})
        return self.snapshot()

    def restore(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Restore a validated durable snapshot for this exact task version."""
        task = snapshot.get("task", {})
        if task.get("id") != self.spec.id or task.get("version") != self.spec.version:
            raise ValueError("task snapshot does not match active task specification")
        checkpoint_id = str(snapshot.get("currentCheckpointId", ""))
        valid_ids = {step.id for step in self.spec.steps}
        ledger = snapshot.get("checkpointLedger")
        if checkpoint_id not in valid_ids or not isinstance(ledger, dict) or set(ledger) != valid_ids:
            raise ValueError("invalid task checkpoint snapshot")
        restored = {}
        for step_id in valid_ids:
            value = ledger[step_id]
            status = value.get("status")
            confidence = value.get("confidence")
            evidence_ids = value.get("evidenceIds", [])
            if status not in {"unknown", "active", "hypothesized", "satisfied"}:
                raise ValueError("invalid task checkpoint status")
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise ValueError("invalid task checkpoint confidence")
            if not isinstance(evidence_ids, list):
                raise ValueError("invalid task checkpoint evidence")
            restored[step_id] = {
                "status": status,
                "confidence": float(confidence),
                "evidenceIds": [str(item) for item in evidence_ids],
                "updatedBy": value.get("updatedBy"),
            }
        step_index = int(snapshot.get("stepIndex", 0))
        if not 0 <= step_index < len(self.spec.steps):
            raise ValueError("invalid task step index")
        decision = snapshot.get("decision")
        if not isinstance(decision, dict) or not {"action", "message", "reason"}.issubset(decision):
            raise ValueError("invalid task decision snapshot")
        self.step_index = step_index
        self.current_checkpoint_id = checkpoint_id
        self.checkpoints = restored
        self.completed = bool(snapshot.get("completed", False))
        self.paused = bool(snapshot.get("paused", False))
        self.observation = snapshot.get("observation") if isinstance(snapshot.get("observation"), dict) else None
        perception = snapshot.get("perception") if isinstance(snapshot.get("perception"), dict) else {}
        self.perception_context_id = str(
            perception.get("contextId") or f"task-context-{uuid4().hex}"
        )
        self.checkpoint_epoch = max(0, int(perception.get("checkpointEpoch", 0)))
        completion_evidence = perception.get("completionEvidence", [])
        self.completion_evidence = (
            [dict(item) for item in completion_evidence if isinstance(item, dict)][-8:]
            if isinstance(completion_evidence, list) else []
        )
        audit = perception.get("audit", [])
        self.perception_audit = (
            [dict(item) for item in reversed(audit) if isinstance(item, dict)][-12:]
            if isinstance(audit, list) else []
        )
        self.announced_memory_ids = {str(item) for item in snapshot.get("announcedMemoryIds", [])}
        self.decision = dict(decision)
        trace = snapshot.get("trace", [])
        self.trace = list(reversed(trace)) if isinstance(trace, list) else []
        self._emit("task.session.restored", {"task": self.spec.id, "checkpoint": checkpoint_id})
        return self.snapshot()

    @property
    def current_step(self) -> TaskStep:
        return next(step for step in self.spec.steps if step.id == self.current_checkpoint_id)

    def allowed_perception_transitions(self) -> list[str]:
        """Return the direct reversible path around the active checkpoint."""
        values = []
        if self.step_index > 0:
            values.append(self.spec.steps[self.step_index - 1].id)
        if self.step_index + 1 < len(self.spec.steps):
            values.append(self.spec.steps[self.step_index + 1].id)
        return values

    def perception_binding(self) -> dict[str, Any]:
        return {
            "contextId": self.perception_context_id,
            "taskId": self.spec.id,
            "taskVersion": self.spec.version,
            "taskProvenance": self.spec.provenance,
            "checkpointId": self.current_checkpoint_id,
            "checkpointEpoch": self.checkpoint_epoch,
            "allowedTransitions": self.allowed_perception_transitions(),
            "completionCriteria": self.current_step.completion,
            "risk": self.current_step.risk,
            "sourceReference": self.current_step.source,
        }

    def record_perception_rejection(
        self,
        reason: str,
        *,
        frame_id: str | None = None,
        context_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        value = {
            "time": self._now(),
            "status": "rejected",
            "reason": str(reason),
            "frameId": frame_id,
            "contextId": context_id,
            "detail": dict(detail or {}),
        }
        self.perception_audit.append(value)
        self.perception_audit = self.perception_audit[-12:]
        self._emit("perception.observation.rejected", value)
        return self.snapshot()

    def ingest(self, value: dict[str, Any]) -> dict[str, Any]:
        observation = self._validate(value)
        valid_steps = {step.id for step in self.spec.steps}
        if observation["step"] not in {*valid_steps, "unknown"}:
            raise ValueError("observation checkpoint is not part of the active task")
        observation["temporalConsistency"] = self._temporal_consistency(observation)
        self.observation = observation
        self.trace.append({"time": self._now(), **observation})
        evidence_event = self._emit("perception.observation.received", observation)
        evidence_id = evidence_event.get("eventId") if isinstance(evidence_event, dict) else None
        observed_checkpoint = self.checkpoints.get(observation["step"])
        if observed_checkpoint and evidence_id:
            observed_checkpoint["evidenceIds"].append(evidence_id)
            observed_checkpoint["confidence"] = observation["confidence"]
        if observation["hazards"]:
            hazard = observation["hazards"][0]
            self.decision = self._decision("emergency", hazard["message"], f"危险检测：{hazard['type']}", hazard["confidence"])
        elif self.paused or self.completed:
            self.decision = self._decision("silent", "陪练已暂停。" if self.paused else "任务已完成。", "会话状态", 1.0)
        elif observation["confidence"] < 0.65:
            self.decision = self._decision("ask", f"我没看清。你正在做“{self.current_step.title}”吗？", "感知置信度不足", observation["confidence"])
        elif observation["step"] not in {self.current_step.id, "unknown"}:
            observed = next((step for step in self.spec.steps if step.id == observation["step"]), None)
            observed_title = observed.title if observed else observation["step"]
            if observed:
                self.checkpoints[observed.id].update({
                    "status": "hypothesized", "confidence": observation["confidence"],
                    "updatedBy": observation["source"],
                })
            self.decision = self._decision(
                "ask",
                f"我看到你可能在做“{observed_title}”，当前计划位置是“{self.current_step.title}”。你是有意调整顺序吗？",
                "路径偏离需要人在回路确认，不能机械纠错或自动跳步",
                observation["confidence"],
            )
        elif observation["completed"] and observation["step"] == self.current_step.id:
            self.decision = self._decision("ask", f"我看到“{self.current_step.title}”可能完成了，请确认。", "推进需要用户确认", observation["confidence"])
        else:
            self.decision = self._decision("silent", "继续观察。", f"当前步骤：{self.current_step.title}", observation["confidence"])
        self._emit("intervention.decision", self.decision, [evidence_id] if evidence_id else [])
        return self.snapshot()

    def _temporal_consistency(self, observation: dict[str, Any]) -> dict[str, Any]:
        frame_id = observation.get("frameId")
        if not (
            frame_id
            and observation.get("completed")
            and observation.get("step") == self.current_checkpoint_id
            and observation.get("confidence", 0) >= 0.65
        ):
            return {"count": 0, "required": 2, "consistent": False}
        self.completion_evidence = [
            item for item in self.completion_evidence
            if item.get("checkpointId") == self.current_checkpoint_id
        ]
        if not any(item.get("frameId") == frame_id for item in self.completion_evidence):
            self.completion_evidence.append({
                "frameId": frame_id,
                "checkpointId": self.current_checkpoint_id,
                "observedAt": observation.get("observedAt") or self._now(),
                "confidence": observation.get("confidence", 0),
                "source": observation.get("source"),
            })
        self.completion_evidence = self.completion_evidence[-8:]
        count = len(self.completion_evidence)
        return {"count": count, "required": 2, "consistent": count >= 2}

    def apply_voice_intent(self, intent: str | None, transcript: str = "") -> dict[str, Any]:
        """Apply a validated semantic label; never infer intent from transcript text."""
        intent = str(intent or "none")
        allowed = {
            "none", "confirm-step", "rollback", "repeat",
            "pause-task", "resume-task", "request-help", "show-reference",
        }
        if intent not in allowed:
            raise ValueError(f"unsupported task voice intent: {intent}")
        if intent == "confirm-step":
            return self.confirm_step("用户语义确认")
        if intent == "rollback":
            self.checkpoints[self.current_checkpoint_id]["status"] = "unknown"
            self.step_index = max(0, self.step_index - 1)
            self.current_checkpoint_id = self.spec.steps[self.step_index].id
            self.checkpoints[self.current_checkpoint_id].update({"status": "active", "updatedBy": "user-rollback"})
            self.completed = False
            self.checkpoint_epoch += 1
            self.completion_evidence = []
            self.decision = self._decision("guide", self.current_step.instruction, "用户请求回退并重新检查", 1.0)
        elif intent == "repeat":
            self.decision = self._decision("guide", self.current_step.instruction, "用户请求重复", 1.0)
        elif intent == "pause-task":
            self.paused = True
            self.decision = self._decision("silent", "已暂停任务指导。", "用户语义控制", 1.0)
        elif intent == "resume-task":
            self.paused = False
            self.decision = self._decision("guide", self.current_step.instruction, "用户恢复", 1.0)
        elif intent == "request-help":
            self.decision = self._decision("guide", self.current_step.instruction, f"知识来源：{self.current_step.source}", 1.0)
        self._emit("interaction.voice.control", {
            "transcript": transcript, "intent": intent, "decision": self.decision,
            "method": "structured-semantic-label",
        })
        return self.snapshot()

    def voice(self, transcript: str) -> dict[str, Any]:
        raise TypeError(
            "TaskSession.voice no longer infers intent from text; use apply_voice_intent"
        )

    def confirm_step(self, source: str) -> dict[str, Any]:
        if self.completed:
            return self.snapshot()
        finished = self.current_step
        self.checkpoint_epoch += 1
        self.completion_evidence = []
        self.checkpoints[finished.id].update({"status": "satisfied", "confidence": 1.0, "updatedBy": source})
        remaining = [step for step in self.spec.steps if self.checkpoints[step.id]["status"] not in {"satisfied", "skipped"}]
        if not remaining:
            self.completed = True
            self.decision = self._decision("guide", "任务完成。请执行最后的安全检查。", source, 1.0)
        else:
            next_step = next((step for step in self.spec.steps[self.step_index + 1:] if step in remaining), remaining[0])
            self.current_checkpoint_id = next_step.id
            self.step_index = self.spec.steps.index(next_step)
            self.checkpoints[next_step.id].update({"status": "active", "updatedBy": source})
            self.decision = self._decision("guide", f"{finished.title}完成。下一步：{self.current_step.instruction}", source, 1.0)
        self._emit("task.checkpoint.confirmed", {"checkpoint": finished.id, "source": source, "completed": self.completed})
        return self.snapshot()

    def apply_memory_hint(self, record: dict[str, Any] | None) -> dict[str, Any]:
        if not record or record["id"] in self.announced_memory_ids:
            return self.snapshot()
        trigger = record.get("trigger", {})
        if trigger.get("task") not in {self.spec.id, self.spec.title}:
            return self.snapshot()
        if trigger.get("step") not in {self.current_step.id, self.current_step.title}:
            return self.snapshot()
        self.announced_memory_ids.add(record["id"])
        self.decision = self._decision("guide", str(record["prompt"]), f"复用长期记忆 #{record['id']}", 1.0)
        self._emit("memory.preference.applied", {"memoryId": record["id"], "checkpoint": self.current_checkpoint_id})
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "task": {"id": self.spec.id, "title": self.spec.title, "version": self.spec.version, "provenance": self.spec.provenance},
            "completed": self.completed,
            "paused": self.paused,
            "stepIndex": self.step_index,
            "progress": sum(item["status"] == "satisfied" for item in self.checkpoints.values()) / len(self.spec.steps),
            "currentCheckpointId": self.current_checkpoint_id,
            "checkpointLedger": {key: {**value, "evidenceIds": list(value["evidenceIds"])} for key, value in self.checkpoints.items()},
            "steps": [
                {**asdict(step), **self.checkpoints[step.id],
                 "status": "done" if self.checkpoints[step.id]["status"] == "satisfied" else self.checkpoints[step.id]["status"]}
                for step in self.spec.steps
            ],
            "observation": self.observation,
            "decision": self.decision,
            "announcedMemoryIds": sorted(self.announced_memory_ids),
            "perception": {
                **self.perception_binding(),
                "completionEvidence": [dict(item) for item in self.completion_evidence],
                "audit": list(reversed(self.perception_audit[-12:])),
                "policy": "evidence-only-user-confirmation-required",
            },
            "trace": list(reversed(self.trace[-8:])),
        }

    def replay_observation(self, kind: str) -> dict[str, Any]:
        """Create an explicit deterministic replay event for the active task."""
        current = self.current_step
        if kind == "progress":
            return {"step": current.id, "completed": False, "confidence": 0.91, "summary": f"回放证据：正在进行{current.title}", "hazards": [], "source": "research-replay/deterministic"}
        if kind == "complete":
            return {"step": current.id, "completed": True, "confidence": 0.88, "summary": f"回放证据：{current.completion}", "hazards": [], "source": "research-replay/deterministic"}
        if kind == "occluded":
            return {"step": "unknown", "completed": False, "confidence": 0.28, "summary": "回放证据：视野被遮挡，无法可靠判断步骤", "hazards": [], "source": "research-replay/deterministic"}
        if kind == "wrong_step":
            other = self.spec.steps[(self.step_index + 1) % len(self.spec.steps)]
            return {"step": other.id, "completed": False, "confidence": 0.86, "summary": f"回放证据：观察到{other.title}，与当前步骤不一致", "hazards": [], "source": "research-replay/deterministic"}
        if kind == "hazard":
            return {"step": current.id, "completed": False, "confidence": 0.95, "summary": f"回放证据：{current.title}出现需立即复核的风险", "hazards": [{"type": "task_safety", "message": f"请暂停{current.title}，先排除现场风险。", "confidence": 0.95}], "source": "research-replay/deterministic"}
        raise ValueError(f"unknown demo observation: {kind}")

    @staticmethod
    def _validate(value: dict[str, Any]) -> dict[str, Any]:
        hazards = value.get("hazards", [])
        if not isinstance(hazards, list):
            raise ValueError("hazards must be a list")
        normalized_hazards = []
        for item in hazards[:3]:
            if not isinstance(item, dict):
                raise ValueError("hazards must contain objects")
            message = " ".join(str(item.get("message", "")).split())[:300]
            if not message:
                raise ValueError("hazard message is required")
            normalized_hazards.append({
                "type": str(item.get("type") or item.get("kind") or "task-safety")[:80],
                "message": message,
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0.0)))),
            })
        evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
        return {
            "observationId": str(value.get("observationId") or value.get("observation_id") or "") or None,
            "step": str(value.get("step") or value.get("checkpointId") or value.get("checkpoint_id") or "unknown"),
            "completed": bool(value.get("completed", evidence.get("completionHypothesis", False))),
            "confidence": max(0.0, min(1.0, float(value.get("confidence", 0.0)))),
            "summary": str(value.get("summary") or evidence.get("summary") or "")[:500],
            "hazards": normalized_hazards,
            "source": str(value.get("source", "external")),
            "frameId": str(value.get("frameId") or evidence.get("frameId") or "") or None,
            "observedAt": str(value.get("observedAt") or value.get("receivedAt") or value.get("received_at") or "") or None,
            "frameAgeMs": value.get("frameAgeMs", value.get("frame_age_ms")),
            "allowedTransitions": list(value.get("allowedTransitions") or value.get("allowed_transitions") or []),
            "contextId": evidence.get("contextId"),
            "visibleEvidence": list(evidence.get("visibleEvidence", []))[:8],
            "hypotheses": list(value.get("hypotheses", []))[:5] if isinstance(value.get("hypotheses", []), list) else [],
        }

    @staticmethod
    def _decision(action: str, message: str, reason: str, confidence: float) -> dict[str, Any]:
        return {"action": action, "message": message, "reason": reason, "confidence": confidence}

    def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        parent_event_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self._event_sink:
            return {}
        return self._event_sink(event_type, "task-control", payload, parent_event_ids or [])

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
