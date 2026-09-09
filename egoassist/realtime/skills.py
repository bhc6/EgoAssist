"""Agent-Skills-compatible discovery with embodied runtime extensions."""

from __future__ import annotations

from egoassist.prompts import prompt_text

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
                    prompt_text('skills/context.1', v0=self.task.spec.realtime_instructions(memories), v1=tool_text)
                )
            return (
                prompt_text('skills/context.2')
            )
        catalog = "、".join(spec.title for spec in self.tasks.values())
        if self.task is None:
            task_guidance = (
                prompt_text('skills/context.3', v0=catalog)
                if catalog
                else prompt_text('skills/context.4')
            )
            return (
                prompt_text('skills/context.5')
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
            + prompt_text('skills/context.6', v0=catalog)
        )

    def live_state_text(self) -> str:
        """Current task runtime state for injection as a separate non-cacheable context layer."""
        if self.task is None:
            return ""
        snapshot = self.task.snapshot()
        completed = [item["title"] for item in snapshot["steps"] if item["status"] == "done"]
        observation = snapshot.get("observation") or {}
        return (
            prompt_text('skills/live_state_text.1', v0=self.task.current_step.id, v1=self.task.current_step.title, v2=completed or ['无'], v3=snapshot['paused'], v4=snapshot['completed'], v5=observation.get('summary', '无结构化观测'), v6=snapshot['decision']['action'], v7=snapshot['decision']['reason'])
        )
