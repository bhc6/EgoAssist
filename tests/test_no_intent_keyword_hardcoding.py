"""Regression guard for the baseline's natural-language intent boundary."""

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_core_state_transitions_do_not_contain_natural_language_keyword_tables():
    scoped = [
        ROOT / "run_egoassist.py",
        ROOT / "egoassist" / "realtime" / "task.py",
        ROOT / "egoassist" / "realtime" / "episodes.py",
    ]
    forbidden_fragments = [
        "any(word in",
        "any(marker in",
        "any(phrase in",
        "is_stop_speaking_command",
        "completion_denied =",
        "completion_confirmed =",
    ]

    for path in scoped:
        source = path.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            assert fragment not in source, f"hidden intent vocabulary in {path.name}: {fragment}"


def test_task_state_machine_rejects_raw_text_inference():
    source = (ROOT / "egoassist" / "realtime" / "task.py").read_text(encoding="utf-8")
    assert "def apply_voice_intent(" in source
    assert "TaskSession.voice no longer infers intent from text" in source


def test_manifest_evidence_is_the_only_documented_lexical_routing_surface():
    source = (ROOT / "egoassist" / "realtime" / "skills.py").read_text(encoding="utf-8")
    assert 'manifest.get("activation_phrases"' in source
    assert "task.title, *task.aliases" in source


def test_dialogue_model_cannot_bypass_the_structured_skill_router():
    source = (ROOT / "run_egoassist.py").read_text(encoding="utf-8")
    assert '"name": "activate_skill"' not in source
    assert "provider-skill-tool-v1" not in source
    assert "provider-skill-activation" not in source
