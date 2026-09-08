from pathlib import Path

import pytest

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.realtime import (
    SkillPackage,
    SkillRegistry,
    SkillRuntime,
    SkillToolExecutor,
    executable_tool_declarations,
    platform_capability_truth,
)


ROOT = Path(__file__).parents[1]


def test_active_skill_injects_declared_policy_even_before_task_selection():
    runtime = SkillRuntime(SkillRegistry(BUILTIN_SKILLS_ROOT), None)
    runtime.activate("assembly-coach", select_default=False)
    assert "exact product manual" in runtime.context([])
    assert "evidence-policy.md" in runtime.context([])
    assert "sha256=" in runtime.context([])
    runtime.select_task("flat-pack-stool")
    assert "exact product manual" in runtime.context([])
    runtime.deactivate()
    assert "exact product manual" not in runtime.context([])


def test_skill_resource_loader_rejects_missing_and_escaping_paths(tmp_path):
    from egoassist.realtime.skill_resources import load_instruction_resources
    for path in ("../outside.md", "references/missing.md"):
        with pytest.raises(ValueError):
            load_instruction_resources(tmp_path, {"instruction_resources": [path]})


def test_multiple_task_mentions_never_reselect_last_alias():
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    route = registry.route("做水果沙拉还是番茄炒蛋，西红柿炒鸡蛋也可以")
    assert route["selectedSkill"] == "kitchen-coach"
    assert route["selectedTask"] is None


def test_bundled_skill_activation_requires_domain_evidence():
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    unrelated = registry.route("请用刚才查到的编号，写一句我可以发给前台的取物说明。")
    assert not registry.accepts_automatic_activation("assembly-coach", unrelated)
    assert not registry.accepts_automatic_activation("kitchen-coach", unrelated)
    assert registry.accepts_automatic_activation("assembly-coach", registry.route("我要组装凳子"))


def test_registry_discovers_agent_skills_and_runtime_switches_workflow():
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    assert set(registry.skills) == {"assembly-coach", "kitchen-coach"}

    runtime = SkillRuntime(registry, "kitchen-coach")
    kitchen_context = runtime.context([])
    assert runtime.task.current_step.id == "prepare"
    assert "刀具" in kitchen_context
    assert {item["id"] for item in runtime.task_summaries()} == {"tomato-eggs", "oatmeal", "fruit-salad"}

    runtime.select_task("oatmeal")
    assert runtime.task.current_step.id == "measure"
    assert "番茄炒蛋" not in runtime.task.spec.realtime_instructions([])

    runtime.activate("assembly-coach")
    assembly_context = runtime.context([])
    assert runtime.task.current_step.id == "inspect"
    assert "密封圈" in assembly_context
    assert kitchen_context != assembly_context


def test_voice_matching_and_custom_tasks_are_runtime_instances():
    runtime = SkillRuntime(SkillRegistry(BUILTIN_SKILLS_ROOT), "kitchen-coach")
    assert runtime.match_task("今天我想做水果沙拉") == "fruit-salad"
    assert runtime.task.spec.title == "水果沙拉"

    runtime.create_task({
        "id": "custom-tea", "title": "泡一杯茶", "version": "test-1",
        "role": "厨房助手", "safety_policy": ["热水风险优先"],
        "steps": [{"id": "boil", "title": "烧水", "instruction": "烧开适量水。", "completion": "水已烧开", "risk": "high", "source": "user"}],
    })
    assert runtime.task.spec.provenance == "user-defined"
    assert runtime.task.spec.title == "泡一杯茶"

    runtime.activate("assembly-coach")
    assert "custom-tea" not in runtime.tasks
    assert runtime.task.spec.id == "water-filter"


def test_skill_changes_tool_surface_and_confirmation_policy():
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    executor = SkillToolExecutor()
    kitchen_tools = registry.get("kitchen-coach").manifest["tools"]
    assembly_tools = registry.get("assembly-coach").manifest["tools"]

    timer = executor.execute("set_timer", {"seconds": 2}, kitchen_tools)
    assert timer["status"] == "unsupported"
    assert "No durable workflow" in timer["reason"]
    pending = executor.execute("request_human_help", {}, kitchen_tools)
    assert pending["status"] == "demo-only"
    assert "confirmation_required" not in {timer["status"], pending["status"]}
    assert executor.audit[-1]["result"]["status"] == "demo-only"
    assert executable_tool_declarations(kitchen_tools) == []
    with pytest.raises(ValueError, match="not available"):
        executor.execute("set_timer", {}, assembly_tools)


def test_capability_truth_does_not_promote_manifest_labels_to_features():
    catalog = {item["id"]: item for item in platform_capability_truth()}
    assert catalog["scoped-memory"]["status"] == "available"
    assert catalog["background-execution"]["status"] == "unsupported"
    assert catalog["timer-delivery"]["status"] == "unsupported"
    assert catalog["human-help"]["status"] == "demo-only"
    assert catalog["structured-perception"]["status"] == "available"
    assert all(item["executable"] == (item["status"] == "available") for item in catalog.values())


def test_tool_contract_drives_defaults_and_fails_closed():
    declaration = [{
        "name": "demo_action", "handler": "test.capture", "requires_confirmation": False,
        "arguments": {
            "type": "object", "additionalProperties": False,
            "properties": {"count": {"type": "integer", "minimum": 1, "default": 3}},
            "required": ["count"],
        },
    }]
    executor = SkillToolExecutor({"test.capture": lambda arguments, _: {"arguments": arguments}})

    assert executor.execute("demo_action", {}, declaration)["arguments"] == {"count": 3}
    with pytest.raises(ValueError, match="undeclared tool arguments"):
        executor.execute("demo_action", {"unexpected": True}, declaration)
    with pytest.raises(ValueError, match="not registered"):
        SkillToolExecutor({}).execute("demo_action", {}, declaration)


def test_tool_executor_rejects_async_handlers_without_audit():
    declaration = [{
        "name": "async_action",
        "handler": "test.async",
        "requires_confirmation": False,
        "arguments": {"type": "object", "properties": {}},
    }]

    async def async_handler(_arguments, _declaration):
        return {"status": "ok"}

    executor = SkillToolExecutor({"test.async": async_handler})

    with pytest.raises(TypeError, match="must be synchronous"):
        executor.execute("async_action", {}, declaration)
    assert executor.audit == []


def test_skill_can_supply_policy_and_tools_without_a_task_catalog(tmp_path):
    root = tmp_path / "context-helper"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: context-helper\ndescription: Adds a scoped interaction policy.\n---\n"
        "Respond within the explicitly activated scope.",
        encoding="utf-8",
    )
    (root / "skill.json").write_text(
        '{"activation_phrases":["context mode"],"tools":[]}',
        encoding="utf-8",
    )

    package = SkillPackage.load(root)
    assert package.task_specs == {}
    assert package.default_task_id is None
    registry = SkillRegistry(tmp_path)
    runtime = SkillRuntime(registry, "context-helper")
    assert runtime.active is True
    assert runtime.task is None
    assert "未声明预设任务" in runtime.context([])


def test_skill_metadata_is_progressively_disclosed():
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    summaries = registry.summaries()
    assert all("instructions" not in item for item in summaries)
    assert "Observe before speaking" in registry.get("kitchen-coach").instructions


def test_runtime_starts_unloaded_and_router_abstains_without_evidence():
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    runtime = SkillRuntime(registry, None)

    assert runtime.active is False
    assert runtime.skill is None
    assert runtime.task is None
    assert runtime.tasks == {}
    assert "没有加载领域 Skill" in runtime.context([])
    assert registry.route("今天天气怎么样")["status"] == "no-match"
    assert registry.route("帮我安装一个软件")["status"] == "no-match"


def test_router_activates_domain_without_silently_selecting_default_task():
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    routed = registry.route("我想做菜，但还没想好做什么")
    assert routed["selectedSkill"] == "kitchen-coach"
    assert routed["selectedTask"] is None
    assert routed["candidates"][0]["evidence"][0]["kind"] == "skill-phrase"

    runtime = SkillRuntime(registry, None)
    runtime.activate(routed["selectedSkill"], select_default=False)
    assert runtime.skill.name == "kitchen-coach"
    assert runtime.task is None
    assert "具体任务尚未确定" in runtime.context([])

    exact = registry.route("请指导我做水果沙拉")
    assert exact["selectedSkill"] == "kitchen-coach"
    assert exact["selectedTask"] == "fruit-salad"


def test_task_without_skill_is_supported(tmp_path):
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    runtime = SkillRuntime(registry, None)

    # create_task must work without any skill loaded
    runtime.create_task({
        "id": "free-form-errand", "title": "买菜", "version": "test-1",
        "role": "生活助手", "safety_policy": ["谨慎跨马路"],
        "steps": [
            {
                "id": "list", "title": "列清单",
                "instruction": "确认购物清单。", "completion": "清单已确认",
                "risk": "low", "source": "user",
            },
        ],
    })
    assert runtime.skill is None
    assert runtime.task is not None
    assert runtime.task.spec.title == "买菜"
    assert runtime.tasks == {"free-form-errand": runtime.task.spec}

    # context() must use the no-skill+task branch
    ctx = runtime.context([])
    assert "没有加载领域 Skill" in ctx
    assert "自定义任务" in ctx
    # must NOT fall through to the general no-skill/no-task prompt
    assert "没有预设任务" not in ctx

    # live_state_text still works without a skill
    assert runtime.live_state_text() != ""

    # activating a skill clears the skill-less custom task (same lifecycle as skill-scoped custom tasks)
    runtime.activate("kitchen-coach")
    assert "free-form-errand" not in runtime.tasks

    # create_task with an active skill that explicitly disables custom tasks must raise
    restricted_root = tmp_path / "restricted-skill"
    restricted_root.mkdir(parents=True, exist_ok=True)
    (restricted_root / "SKILL.md").write_text(
        "---\nname: restricted-skill\ndescription: No custom tasks allowed.\n---\nFixed-scope skill.",
        encoding="utf-8",
    )
    (restricted_root / "skill.json").write_text(
        '{"allow_custom_tasks": false, "tools": []}',
        encoding="utf-8",
    )
    restricted_registry = SkillRegistry(restricted_root.parent)
    runtime2 = SkillRuntime(restricted_registry, None)
    runtime2.activate("restricted-skill")
    with pytest.raises(ValueError, match="custom tasks are disabled"):
        runtime2.create_task({
            "id": "some-task", "title": "Some Task", "version": "1",
            "role": "Assistant", "safety_policy": [],
            "steps": [
                {
                    "id": "step-one", "title": "Do it",
                    "instruction": "Do the thing.", "completion": "Done",
                    "risk": "low", "source": "user",
                },
            ],
        })
