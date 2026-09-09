"""Agent-Skills-compatible discovery with embodied runtime extensions."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .capabilities import executable_tool_declarations
from .task import TaskSession, TaskSpec
from .skill_package import SkillPackage

class SkillRegistry:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.skills = {}
        for path in sorted(self.root.iterdir()):
            if not path.is_dir() or not (path / "SKILL.md").exists():
                continue
            package = SkillPackage.load(path)
            if package.name in self.skills:
                raise ValueError(f"duplicate skill name: {package.name}")
            self.skills[package.name] = package
        if not self.skills:
            raise ValueError(f"no skills found under {self.root}")

    def get(self, name: str) -> SkillPackage:
        try:
            return self.skills[name]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {name}") from exc

    def summaries(self) -> list[dict[str, Any]]:
        return [skill.public_summary() for skill in self.skills.values()]

    def routing_catalog(self) -> list[dict[str, str]]:
        return [{"name": skill.name, "description": skill.description} for skill in self.skills.values()]

    def accepts_automatic_activation(self, name: str, route: dict[str, Any]) -> bool:
        package = self.get(name)
        if not package.manifest.get("automatic_activation_requires_manifest_evidence", False):
            return True
        return route.get("selectedSkill") == name and route.get("status") == "matched"

    def route(self, text: str) -> dict[str, Any]:
        """Return an auditable lexical route; ambiguous and unknown input abstain."""
        normalized = text.casefold().strip()
        candidates = []
        for skill in self.skills.values():
            evidence = []
            matched_tasks = set()
            for phrase in map(str, skill.manifest.get("activation_phrases", [])):
                if phrase_matches(phrase, normalized):
                    evidence.append({"kind": "skill-phrase", "value": phrase, "weight": 0.7})
            for task in skill.task_specs.values():
                for phrase in (task.title, *task.aliases):
                    if phrase_matches(phrase, normalized):
                        evidence.append({"kind": "task-name", "value": phrase, "weight": 1.0})
                        matched_tasks.add(task.id)
            score = max((item["weight"] for item in evidence), default=0.0)
            candidates.append({
                "skill": skill.name,
                "score": score,
                "evidence": evidence,
                "task": next(iter(matched_tasks)) if len(matched_tasks) == 1 else None,
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


def phrase_matches(phrase: str, text: str) -> bool:
    # English aliases must not match inside unrelated words (cook / cookie).
    phrase = phrase.casefold()
    left = r"(?<![a-z0-9_])" if phrase[:1].isascii() and phrase[:1].isalnum() else ""
    right = r"(?![a-z0-9_])" if phrase[-1:].isascii() and phrase[-1:].isalnum() else ""
    return bool(re.search(left + re.escape(phrase) + right, text.casefold()))


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
        if self.skill is not None:
            return {**self.skill.task_specs, **self.custom_tasks}
        return dict(self.custom_tasks)

    def select_task(self, task_id: str) -> None:
        try:
            spec = self.tasks[task_id]
        except KeyError as exc:
            ctx = self.skill.name if self.skill is not None else "no-skill"
            raise ValueError(f"unknown task for {ctx}: {task_id}") from exc
        self.task = TaskSession(spec, event_sink=self._event_sink)

    def create_task(self, payload: dict[str, Any] | TaskSpec) -> None:
        if self.skill is not None and not self.skill.manifest.get("allow_custom_tasks", False):
            raise ValueError(f"custom tasks are disabled for skill: {self.skill.name}")
        spec = payload if isinstance(payload, TaskSpec) else TaskSpec.from_dict(payload, provenance="user-defined")
        self.custom_tasks[spec.id] = spec
        self.select_task(spec.id)

    def match_task(self, text: str) -> str | None:
        normalized = text.casefold()
        matches = [
            spec.id for spec in self.tasks.values()
            if any(phrase_matches(candidate, normalized) for candidate in (spec.title, *spec.aliases))
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
        *,
        include_skill_policy: bool = True,
    ) -> str:
        if self.skill is None:
            if self.task is not None:
                tools = executable_tool_declarations(tool_declarations or [])
                tool_text = "\n".join(
                    f"- {tool['name']}: {tool.get('description', '')}; 需要确认={tool.get('requires_confirmation', True)}"
                    for tool in tools
                ) or "- 无额外工具"
                return (
                    "你是 EgoAssist，一个通用实时多模态助手。当前没有加载领域 Skill，用户已指定自定义任务。\n\n"
                    f"{self.task.spec.realtime_instructions(memories)}\n可用工具：\n{tool_text}"
                )
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
                "你是一个通用实时具身助手。领域 Skill 已加载。"
                + ("具体任务尚未确定。\n\n" if catalog else "这是无固定步骤的对话工作流。\n\n")
                + (f"{self.skill.instruction_context()}\n\n" if include_skill_policy else "")
                + task_guidance
                + "\n可用领域工具：\n"
                + ("\n".join(
                    f"- {tool['name']}: {tool.get('description', '')}"
                    for tool in executable_tool_declarations(
                        self.skill.manifest.get("tools", []) if tool_declarations is None else tool_declarations
                    )
                ) or "- 无额外工具")
            )
        return (
            self.skill.realtime_instructions(
                self.task.spec, memories, tool_declarations, include_skill_policy=include_skill_policy,
            )
            + f"\n当前 Skill 可切换任务：{catalog}。用户明确说出其中一个任务名称时切换；"
              "若用户目标不是当前任务，先澄清或请求创建任务，绝不把默认任务冒充为用户目标。"
        )

    def live_state_text(self) -> str:
        """Current task runtime state for injection as a separate non-cacheable context layer."""
        if self.task is None:
            return ""
        snapshot = self.task.snapshot()
        completed = [item["title"] for item in snapshot["steps"] if item["status"] == "done"]
        observation = snapshot.get("observation") or {}
        return (
            f"<runtime_state>当前步骤={self.task.current_step.id}:{self.task.current_step.title}; "
            f"已确认完成={completed or ['无']}; 暂停={snapshot['paused']}; 总任务完成={snapshot['completed']}; "
            f"最近观测={observation.get('summary', '无结构化观测')}; "
            f"控制面决策={snapshot['decision']['action']}:{snapshot['decision']['reason']}</runtime_state>"
        )
