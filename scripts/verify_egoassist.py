"""One-command provider-neutral verification excluding browser hardware access."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.realtime import SkillRegistry, SkillRuntime, SkillToolExecutor


ROOT = Path(__file__).parents[1]


def main() -> None:
    expected_python = (ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    if Path(sys.executable).resolve() != expected_python:
        raise SystemExit("Run with .\\.venv\\Scripts\\python.exe")

    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    generic = SkillRuntime(registry, None)
    executor = SkillToolExecutor()
    skill_checks = {}
    for name, package in registry.skills.items():
        runtime = SkillRuntime(registry, name)
        phrase = next(iter(package.manifest.get("activation_phrases", [])), "")
        route = registry.route(phrase) if phrase else registry.route("")
        task = runtime.task
        replay_action = None
        if task:
            replay_action = task.ingest(task.replay_observation("occluded"))["decision"]["action"]
        tool_result = None
        if package.manifest.get("tools"):
            declaration = package.manifest["tools"][0]
            tool_result = executor.execute(
                declaration["name"], {}, package.manifest["tools"], confirmed=True,
            )["status"]
        skill_checks[name] = {
            "routeStatus": route["status"],
            "routeSelectedSkill": route["selectedSkill"],
            "taskCount": len(package.task_specs),
            "defaultTask": package.default_task_id,
            "replayAction": replay_action,
            "toolResult": tool_result,
        }

    cross_skill_tool_blocked = None
    packages = list(registry.skills.values())
    for source in packages:
        for target in packages:
            if source is target:
                continue
            target_names = {item["name"] for item in target.manifest.get("tools", [])}
            unique = next((item for item in source.manifest.get("tools", []) if item["name"] not in target_names), None)
            if unique:
                try:
                    executor.execute(unique["name"], {}, target.manifest.get("tools", []), confirmed=True)
                except ValueError:
                    cross_skill_tool_blocked = True
                else:
                    cross_skill_tool_blocked = False
                break
        if cross_skill_tool_blocked is not None:
            break

    tests = subprocess.run(
        [sys.executable, "-m", "pytest"], cwd=ROOT, check=False,
        capture_output=True, text=True,
    )
    summary = {
        "python": sys.version.split()[0],
        "genericStart": {"skill": generic.skill, "task": generic.task},
        "skillChecks": skill_checks,
        "crossSkillToolBlocked": cross_skill_tool_blocked,
        "testsPassed": tests.returncode == 0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if tests.returncode:
        print(tests.stdout)
        print(tests.stderr, file=sys.stderr)
        raise SystemExit(tests.returncode)


if __name__ == "__main__":
    main()
