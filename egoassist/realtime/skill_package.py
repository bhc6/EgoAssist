"""Loading and disclosure of versioned domain policies, resources and task specs."""

from __future__ import annotations

from egoassist.prompts import prompt_text

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .capabilities import executable_tool_declarations, public_tool_declaration, skill_capability_truth
from .task import TaskSpec, bounded_text
from .skill_resources import SkillResource, load_instruction_resources


@dataclass(frozen=True)
class SkillPackage:
    name: str
    description: str
    instructions: str
    root: Path
    manifest: dict[str, Any]
    task_specs: dict[str, TaskSpec]
    default_task_id: str | None
    instruction_resources: tuple[SkillResource, ...] = ()
    content_hash: str = ""

    @classmethod
    def load(cls, root: str | Path) -> "SkillPackage":
        root = Path(root).resolve()
        skill_path = root / "SKILL.md"
        if skill_path.stat().st_size > 32768:
            raise ValueError(f"SKILL.md exceeds the 32 KiB budget: {root}")
        skill_text = skill_path.read_text(encoding="utf-8-sig")
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", skill_text, re.S)
        if not match:
            raise ValueError(f"invalid SKILL.md frontmatter: {root}")
        try:
            metadata = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid Skill YAML: {root}") from exc
        if not isinstance(metadata, dict) or not metadata.get("name") or not metadata.get("description"):
            raise ValueError(f"skill requires name and description: {root}")
        name = bounded_text(metadata["name"], "skill name", 64)
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError(f"invalid skill name: {name}")
        description = bounded_text(metadata["description"], "skill description", 1024)
        instructions = bounded_text(match.group(2), "skill instructions", 32768)
        manifest_path = root / "skill.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        if not isinstance(manifest, dict):
            raise ValueError(f"skill manifest must be an object: {root}")
        for key in ("allow_custom_tasks", "automatic_activation_requires_manifest_evidence"):
            if key in manifest and type(manifest[key]) is not bool:
                raise ValueError(f"{key} must be a boolean: {root}")
        if "version" in manifest:
            bounded_text(manifest["version"], "skill version", 64)
        phrases = manifest.get("activation_phrases", [])
        if not isinstance(phrases, list) or len(phrases) > 64:
            raise ValueError("activation_phrases must contain at most 64 strings")
        for phrase in phrases:
            bounded_text(phrase, "activation phrase", 160)
        if manifest.get("tasks") and manifest.get("task"):
            raise ValueError("declare tasks or task, not both")
        task_pattern = manifest.get("tasks")
        declared_path = task_pattern or manifest.get("task")
        if declared_path is not None:
            bounded_text(declared_path, "task path", 200)
            if Path(declared_path).is_absolute() or ".." in Path(declared_path).parts:
                raise ValueError("task path must stay within the skill root")
        if task_pattern:
            task_paths = sorted(root.glob(str(task_pattern)))
        elif manifest.get("task"):
            task_paths = [root / str(manifest["task"])]
        else:
            task_paths = []
        if declared_path and not task_paths:
            raise ValueError(f"skill task pattern matched no files: {root}")
        if any(not path.resolve().is_relative_to(root) for path in task_paths):
            raise ValueError("task path must stay within the skill root")
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
        resources = load_instruction_resources(root, manifest)
        fingerprint = json.dumps({
            "metadata": metadata, "instructions": instructions, "manifest": manifest,
            "tasks": {key: spec.to_dict() for key, spec in task_specs.items()},
            "resources": [{"path": item.path, "sha256": item.sha256} for item in resources],
        }, ensure_ascii=False, sort_keys=True, default=str)
        return cls(
            name=name,
            description=description,
            instructions=instructions,
            root=root,
            manifest=manifest,
            task_specs=task_specs,
            default_task_id=default_task_id,
            instruction_resources=resources,
            content_hash=hashlib.sha256(fingerprint.encode("utf-8")).hexdigest(),
        )

    def instruction_context(self) -> str:
        return (prompt_text('skill_package/instruction_context.1', v0=self.name, v1=self.instructions)
                + "\n".join(resource.context() for resource in self.instruction_resources))

    def realtime_instructions(
        self,
        task_spec: TaskSpec,
        memories: list[dict[str, Any]],
        tool_declarations: list[dict[str, Any]] | None = None,
        *,
        include_skill_policy: bool = True,
    ) -> str:
        tools = executable_tool_declarations(
            self.manifest.get("tools", []) if tool_declarations is None else tool_declarations
        )
        tool_text = "\n".join(
            f"- {tool['name']}: {tool.get('description', '')}; 需要确认={tool.get('requires_confirmation', True)}"
            for tool in tools
        ) or "- 无额外工具"
        return (
            prompt_text('skill_package/realtime_instructions.1', v0=self.instruction_context() if include_skill_policy else '', v1=task_spec.realtime_instructions(memories), v2=tool_text)
        )

    def public_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.manifest.get("version"),
            "contentHash": self.content_hash,
            "workflowMode": "procedural" if self.task_specs else "conversational",
            "instructionResources": [
                {"path": resource.path, "sha256": resource.sha256}
                for resource in self.instruction_resources
            ],
            "defaultTask": self.default_task_id,
            "allowCustomTasks": bool(self.manifest.get("allow_custom_tasks", False)),
            "automaticActivationRequiresManifestEvidence": bool(
                self.manifest.get("automatic_activation_requires_manifest_evidence", False)
            ),
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
