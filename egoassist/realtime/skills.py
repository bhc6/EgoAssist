"""Agent-Skills-compatible discovery with embodied runtime extensions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .capabilities import (
    executable_tool_declarations,
    public_tool_declaration,
    skill_capability_truth,
)
from .task import TaskSession, TaskSpec


@dataclass(frozen=True)
class SkillPackage:
    name: str
    description: str
    instructions: str
    root: Path
    manifest: dict[str, Any]
    task_specs: dict[str, TaskSpec]
    default_task_id: str | None

    @classmethod
    def load(cls, root: str | Path) -> "SkillPackage":
        root = Path(root)
        skill_text = (root / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", skill_text, re.S)
        if not match:
            raise ValueError(f"invalid SKILL.md frontmatter: {root}")
        metadata = {}
        for line in match.group(1).splitlines():
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip().strip("\"'")
        if not metadata.get("name") or not metadata.get("description"):
            raise ValueError(f"skill requires name and description: {root}")
        manifest_path = root / "skill.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        task_pattern = manifest.get("tasks")
        if task_pattern:
            task_paths = sorted(root.glob(str(task_pattern)))
        elif manifest.get("task"):
            task_paths = [root / str(manifest["task"])]
        else:
            task_paths = []
        if any(not path.is_file() for path in task_paths):
            raise ValueError(f"skill references a missing task spec: {root}")
        task_specs = {spec.id: spec for spec in (TaskSpec.load(path) for path in task_paths)}
        if len(task_specs) != len(task_paths):
            raise ValueError(f"duplicate task id under skill: {root}")
        raw_default = manifest.get("default_task", next(iter(task_specs), None))
        default_task_id = str(raw_default) if raw_default is not None else None
        if default_task_id is not None and default_task_id not in task_specs:
            raise ValueError(f"unknown default task {default_task_id}: {root}")
        tools = manifest.get("tools", [])
        if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
            raise ValueError(f"skill tools must be a list of declarations: {root}")
        names = [str(tool.get("name", "")) for tool in tools]
        if any(not name for name in names) or len(names) != len(set(names)):
            raise ValueError(f"skill tool names must be non-empty and unique: {root}")
        if any(not tool.get("handler") or not isinstance(tool.get("arguments", {}), dict) for tool in tools):
            raise ValueError(f"each skill tool requires a handler and object argument schema: {root}")
        return cls(
            name=metadata["name"],
            description=metadata["description"],
            instructions=match.group(2).strip(),
            root=root,
            manifest=manifest,
            task_specs=task_specs,
            default_task_id=default_task_id,
        )

    def realtime_instructions(
        self,
        task_spec: TaskSpec,
        memories: list[dict[str, Any]],
        tool_declarations: list[dict[str, Any]] | None = None,
    ) -> str:
        tools = executable_tool_declarations(
            self.manifest.get("tools", []) if tool_declarations is None else tool_declarations
        )
        tool_text = "\n".join(
            f"- {tool['name']}: {tool.get('description', '')}; 需要确认={tool.get('requires_confirmation', True)}"
            for tool in tools
        ) or "- 无额外工具"
        return (
            "你是一个通用实时具身助手。以下 Skill 已被用户激活；它可以改变你的领域工作流，但不能覆盖用户控制、隐私与系统安全边界。\n\n"
            f"<skill name=\"{self.name}\">\n{self.instructions}\n</skill>\n\n"
            f"{task_spec.realtime_instructions(memories)}\n可用领域工具：\n{tool_text}"
        )

    def public_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "defaultTask": self.default_task_id,
            "allowCustomTasks": bool(self.manifest.get("allow_custom_tasks", False)),
            "tasks": [
                {"id": task.id, "title": task.title, "version": task.version, "provenance": task.provenance}
                for task in self.task_specs.values()
            ],
            "tools": [
                public_tool_declaration(tool)
                for tool in self.manifest.get("tools", [])
            ],
            "capabilities": self.manifest.get("capabilities", []),
            "capabilityTruth": skill_capability_truth(self.manifest),
        }


class SkillRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.skills = {
            package.name: package
            for package in (
                SkillPackage.load(path)
                for path in sorted(self.root.iterdir())
                if path.is_dir() and (path / "SKILL.md").exists()
            )
        }
        if not self.skills:
            raise ValueError(f"no skills found under {self.root}")

    def get(self, name: str) -> SkillPackage:
        try:
            return self.skills[name]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {name}") from exc

    def summaries(self) -> list[dict[str, Any]]:
        return [skill.public_summary() for skill in self.skills.values()]

    def route(self, text: str) -> dict[str, Any]:
        """Return an auditable lexical route; ambiguous and unknown input abstain."""
        normalized = text.casefold().strip()
        candidates = []
        for skill in self.skills.values():
            evidence = []
            matched_task = None
            for phrase in map(str, skill.manifest.get("activation_phrases", [])):
                if phrase.casefold() in normalized:
                    evidence.append({"kind": "skill-phrase", "value": phrase, "weight": 0.7})
            for task in skill.task_specs.values():
                for phrase in (task.title, *task.aliases):
                    if phrase.casefold() in normalized:
                        evidence.append({"kind": "task-name", "value": phrase, "weight": 1.0})
                        matched_task = task.id if matched_task in {None, task.id} else None
            score = max((item["weight"] for item in evidence), default=0.0)
            candidates.append({
                "skill": skill.name,
                "score": score,
                "evidence": evidence,
                "task": matched_task,
                "selected": False,
                "rejectionReasons": [],
            })
        ranked = sorted(candidates, key=lambda item: (-item["score"], item["skill"]))
        top_score = ranked[0]["score"] if ranked else 0.0
        winners = [item for item in ranked if item["score"] == top_score and top_score > 0]
        selected = winners[0] if len(winners) == 1 else None
        for item in ranked:
            item["selected"] = bool(selected and item["skill"] == selected["skill"])
            if not item["evidence"]:
                item["rejectionReasons"].append("no-evidence")
            elif selected and not item["selected"]:
                item["rejectionReasons"].append("lower-score")
            elif len(winners) > 1:
                item["rejectionReasons"].append("ambiguous-top-score")
        return {
            "method": "skill-manifest-lexical-v1",
            "input": text,
            "status": "matched" if selected else "ambiguous" if winners else "no-match",
            "selectedSkill": selected["skill"] if selected else None,
            "selectedTask": selected["task"] if selected else None,
            "candidates": ranked,
            "claimBoundary": "Deterministic manifest evidence; no semantic intent model.",
        }


class SkillRuntime:
    def __init__(
        self,
        registry: SkillRegistry,
        active: str | None,
        event_sink: Callable[..., Any] | None = None,
    ) -> None:
        self.registry = registry
        self._event_sink = event_sink
        self.skill: SkillPackage | None = None
        self.task: TaskSession | None = None
        self.custom_tasks: dict[str, TaskSpec] = {}
        if active:
            self.activate(active)

    @property
    def active(self) -> bool:
        return self.skill is not None

    def set_event_sink(self, event_sink: Callable[..., Any] | None) -> None:
        self._event_sink = event_sink
        if self.task is not None:
            self.task.set_event_sink(event_sink)

    def activate(self, name: str, *, select_default: bool = True) -> None:
        self.skill = self.registry.get(name)
        self.custom_tasks = {}
        self.task = None
        if select_default and self.skill.default_task_id is not None:
            self.select_task(self.skill.default_task_id)

    def deactivate(self) -> None:
        self.skill = None
        self.task = None
        self.custom_tasks = {}

    def _require_skill(self) -> SkillPackage:
        if self.skill is None:
            raise ValueError("no active skill")
        return self.skill

    def _require_task(self) -> TaskSession:
        if self.task is None:
            raise ValueError("no active task")
        return self.task

    @property
    def tasks(self) -> dict[str, TaskSpec]:
        return {**self.skill.task_specs, **self.custom_tasks} if self.skill else {}

    def select_task(self, task_id: str) -> None:
        skill = self._require_skill()
        try:
            spec = self.tasks[task_id]
        except KeyError as exc:
            raise ValueError(f"unknown task for {skill.name}: {task_id}") from exc
        self.task = TaskSession(spec, event_sink=self._event_sink)

    def create_task(self, payload: dict[str, Any] | TaskSpec) -> None:
        skill = self._require_skill()
        if not skill.manifest.get("allow_custom_tasks", False):
            raise ValueError(f"custom tasks are disabled for skill: {skill.name}")
        spec = payload if isinstance(payload, TaskSpec) else TaskSpec.from_dict(payload, provenance="user-defined")
        self.custom_tasks[spec.id] = spec
        self.select_task(spec.id)

    def match_task(self, text: str) -> str | None:
        normalized = text.casefold()
        matches = [
            spec.id for spec in self.tasks.values()
            if any(candidate.casefold() in normalized for candidate in (spec.title, *spec.aliases))
        ]
        if len(matches) == 1:
            self.select_task(matches[0])
            return matches[0]
        return None

    def task_summaries(self) -> list[dict[str, Any]]:
        return [
            {"id": spec.id, "title": spec.title, "version": spec.version, "provenance": spec.provenance}
            for spec in self.tasks.values()
        ]

    def context(
        self,
        memories: list[dict[str, Any]],
        tool_declarations: list[dict[str, Any]] | None = None,
    ) -> str:
        if self.skill is None:
            return (
                "你是 EgoAssist，一个通用实时多模态助手。当前没有加载领域 Skill，也没有预设任务。"
                "先理解用户实际目标；自然回答一般问题。只有证据明确匹配可用 Skill 时才加载它，"
                "不因看到某类物体就擅自推断用户目标或加载领域工作流。看不清或意图不明确时简短询问。"
                "持续接收用户麦克风和相机画面；允许用户与助手同时说话。只输出面向用户的最终话语，"
                "不要说出内部决策、保持安静之类的元话语。"
            )
        catalog = "、".join(spec.title for spec in self.tasks.values())
        if self.task is None:
            task_guidance = (
                f"可选任务：{catalog}。先确认用户的具体目标；不要把默认任务冒充为用户目标。"
                if catalog
                else "该 Skill 未声明预设任务；直接按其能力边界响应，不虚构步骤或任务状态。"
            )
            return (
                "你是一个通用实时具身助手。以下领域 Skill 已因用户意图证据被加载，但具体任务尚未确定。\n\n"
                f"<skill name=\"{self.skill.name}\">\n{self.skill.instructions}\n</skill>\n\n"
                + task_guidance
            )
        snapshot = self.task.snapshot()
        completed = [item["title"] for item in snapshot["steps"] if item["status"] == "done"]
        observation = snapshot.get("observation") or {}
        live_state = (
            f"\n<runtime_state>当前步骤={self.task.current_step.id}:{self.task.current_step.title}; "
            f"已确认完成={completed or ['无']}; 暂停={snapshot['paused']}; 总任务完成={snapshot['completed']}; "
            f"最近观测={observation.get('summary', '无结构化观测')}; "
            f"控制面决策={snapshot['decision']['action']}:{snapshot['decision']['reason']}</runtime_state>"
        )
        return (
            self.skill.realtime_instructions(self.task.spec, memories, tool_declarations)
            + f"\n当前 Skill 可切换任务：{catalog}。用户明确说出其中一个任务名称时切换；"
              "若用户目标不是当前任务，先澄清或请求创建任务，绝不把默认任务冒充为用户目标。"
            + live_state
        )
