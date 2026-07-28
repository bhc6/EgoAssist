"""Generic task specification and risk-controlled guidance state machine."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


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
        if not isinstance(payload, dict) or not required.issubset(payload) or not payload["steps"]:
            raise ValueError("invalid task specification")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", str(payload["id"])):
            raise ValueError("task id must use 2-64 lowercase letters, digits, hyphens or underscores")
        if not isinstance(payload["steps"], list) or len(payload["steps"]) > 16:
            raise ValueError("task requires 1-16 steps")
        steps: list[TaskStep] = []
        seen: set[str] = set()
        for raw in payload["steps"]:
            if not isinstance(raw, dict):
                raise ValueError("each task step must be an object")
            missing = {"id", "title", "instruction", "completion"} - set(raw)
            if missing:
                raise ValueError(f"task step missing: {', '.join(sorted(missing))}")
            step = TaskStep(**raw)
            if step.id in seen or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", step.id):
                raise ValueError(f"invalid or duplicate step id: {step.id}")
            if step.risk not in {"low", "medium", "high"}:
                raise ValueError(f"invalid risk level: {step.risk}")
            if not all((step.title.strip(), step.instruction.strip(), step.completion.strip())):
                raise ValueError("task step text cannot be empty")
            seen.add(step.id)
            steps.append(step)
        return cls(
            id=str(payload["id"]),
            title=str(payload["title"]),
            version=str(payload["version"]),
            role=str(payload["role"]),
            safety_policy=tuple(map(str, payload.get("safety_policy", []))),
            steps=tuple(steps),
            aliases=tuple(map(str, payload.get("aliases", []))),
            provenance=str(payload.get("provenance", provenance)),
        )

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


class TaskSession:
    def __init__(
        self,
        spec: TaskSpec,
        event_sink: Callable[[str, str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.spec = spec
        self._event_sink = event_sink
        self.reset()

    def reset(self) -> dict[str, Any]:
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

    @property
    def current_step(self) -> TaskStep:
        return next(step for step in self.spec.steps if step.id == self.current_checkpoint_id)

    def ingest(self, value: dict[str, Any]) -> dict[str, Any]:
        observation = self._validate(value)
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

    def voice(self, transcript: str) -> dict[str, Any]:
        text = transcript.strip().lower()
        completion_denied = any(word in text for word in ("没完成", "没有完成", "还没", "未完成", "别到下一步"))
        completion_question = any(word in text for word in ("下一步怎么", "下一步是什么", "完成了吗", "算完成吗"))
        completion_confirmed = any(word in text for word in ("完成了", "已经好了", "这步好了", "可以下一步了", "done"))
        if completion_confirmed and not completion_denied and not completion_question:
            return self.confirm_step("用户语音确认")
        if any(word in text for word in ("上一步", "回退", "退一步", "go back")):
            self.checkpoints[self.current_checkpoint_id]["status"] = "unknown"
            self.step_index = max(0, self.step_index - 1)
            self.current_checkpoint_id = self.spec.steps[self.step_index].id
            self.checkpoints[self.current_checkpoint_id].update({"status": "active", "updatedBy": "user-rollback"})
            self.completed = False
            self.decision = self._decision("guide", self.current_step.instruction, "用户请求回退并重新检查", 1.0)
        elif any(word in text for word in ("重复", "再说", "repeat", "没听清")):
            self.decision = self._decision("guide", self.current_step.instruction, "用户请求重复", 1.0)
        elif any(word in text for word in ("暂停", "停止", "stop", "pause")):
            self.paused = True
            self.decision = self._decision("silent", "已暂停。说“继续”恢复陪练。", "用户控制", 1.0)
        elif any(word in text for word in ("继续", "恢复", "resume")):
            self.paused = False
            self.decision = self._decision("guide", self.current_step.instruction, "用户恢复", 1.0)
        elif any(word in text for word in ("帮助", "怎么做", "下一步", "help")):
            self.decision = self._decision("guide", self.current_step.instruction, f"知识来源：{self.current_step.source}", 1.0)
        self._emit("interaction.voice.control", {"transcript": transcript, "decision": self.decision})
        return self.snapshot()

    def confirm_step(self, source: str) -> dict[str, Any]:
        if self.completed:
            return self.snapshot()
        finished = self.current_step
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
        return {
            "step": str(value.get("step", "unknown")),
            "completed": bool(value.get("completed", False)),
            "confidence": max(0.0, min(1.0, float(value.get("confidence", 0.0)))),
            "summary": str(value.get("summary", ""))[:300],
            "hazards": hazards[:3],
            "source": str(value.get("source", "external")),
            "frameId": str(value.get("frameId", "")) or None,
            "observedAt": str(value.get("observedAt", "")) or None,
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
