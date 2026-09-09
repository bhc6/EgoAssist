import json
import shutil

import pytest

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.realtime import SkillPackage, SkillRegistry, SkillRuntime


def package_at(root, *, name="example", description="Example policy.", manifest=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nUse supplied evidence only.\n", encoding="utf-8",
    )
    (root / "skill.json").write_text(json.dumps(manifest or {}), encoding="utf-8")
    return root


def test_standard_yaml_description_is_loaded_and_stably_fingerprinted(tmp_path):
    path = package_at(tmp_path / "example", description=">-\n  First line\n  second line.")
    package = SkillPackage.load(path)
    assert package.description == "First line second line."
    assert package.content_hash == SkillPackage.load(path).content_hash
    assert len(package.content_hash) == 64
    assert package.public_summary()["workflowMode"] == "conversational"
    assert "instructions" not in package.public_summary()


@pytest.mark.parametrize("manifest", [
    {"tasks": "missing/*.json"}, {"task": "../outside.json"},
    {"activation_phrases": "cook"}, {"activation_phrases": [""]},
    {"allow_custom_tasks": "false"}, {"automatic_activation_requires_manifest_evidence": "false"},
])
def test_invalid_package_contract_fails_at_registration(tmp_path, manifest):
    path = package_at(tmp_path / "example", manifest=manifest)
    with pytest.raises(ValueError):
        SkillPackage.load(path)


def test_duplicate_skill_names_are_not_silently_overwritten(tmp_path):
    package_at(tmp_path / "first")
    package_at(tmp_path / "second")
    with pytest.raises(ValueError, match="duplicate skill name"):
        SkillRegistry(tmp_path)


def test_fingerprint_tracks_policy_task_and_resource_changes_but_not_install_path(tmp_path):
    path = tmp_path / "packing-coach"
    shutil.copytree(BUILTIN_SKILLS_ROOT / "packing-coach", path)
    first = SkillPackage.load(path).content_hash
    assert first == SkillPackage.load(BUILTIN_SKILLS_ROOT / "packing-coach").content_hash
    for filename in ("SKILL.md", "references/item-evidence.md", "tasks/commute-check.json"):
        target = path / filename
        original = target.read_text(encoding="utf-8")
        if filename.endswith(".json"):
            payload = json.loads(original)
            payload["version"] = "changed"
            updated = json.dumps(payload)
        else:
            updated = original + "\nChanged policy.\n"
        target.write_text(updated, encoding="utf-8")
        assert SkillPackage.load(path).content_hash != first
        target.write_text(original, encoding="utf-8")


def test_english_activation_requires_word_boundaries():
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    assert registry.route("Explain a browser cookie")['selectedSkill'] is None
    assert registry.route("What is a disassembler?")['selectedSkill'] is None
    assert registry.route("cook fruit salad")['selectedTask'] == 'fruit-salad'


def test_new_skills_have_distinct_workflows_and_bounded_disclosure():
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    assert all(set(item) == {"name", "description"} for item in registry.routing_catalog())
    route = registry.route("帮我做通勤出门核对")
    assert route['selectedSkill'] == 'packing-coach'
    assert route['selectedTask'] == 'commute-check'
    assert registry.route("帮我整理现场记录")['selectedSkill'] == 'field-notes'
    runtime = SkillRuntime(registry, 'field-notes')
    assert runtime.task is None
    assert '具体任务尚未确定' not in runtime.context([])
    assert '无固定步骤' in runtime.context([])
    assert 'note-contract.md' in runtime.context([])
    assert 'note-contract.md' not in runtime.context([], include_skill_policy=False)
    assert '<skill name=' not in runtime.context([], include_skill_policy=False)
    assert registry.get('packing-coach').default_task_id is None
    assert registry.route("我昨天写过现场笔记")['selectedSkill'] is None


def test_conversational_skill_exposes_only_executable_tool_instructions(tmp_path):
    registry = SkillRegistry(package_at(tmp_path / "example").parent)
    runtime = SkillRuntime(registry, "example")
    declarations = [
        {"name": "supported", "arguments": {}},
        {"name": "offline", "availability": {"status": "unsupported"}},
    ]
    text = runtime.context([], declarations)
    assert 'supported' in text
    assert 'offline' not in text
