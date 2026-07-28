from pathlib import Path

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.realtime import TaskSession, TaskSpec
import pytest


ROOT = Path(__file__).parents[1]
TASK = BUILTIN_SKILLS_ROOT / "kitchen-coach" / "tasks" / "tomato-eggs.json"


def test_task_spec_loads_and_builds_realtime_context():
    spec = TaskSpec.load(TASK)
    context = spec.realtime_instructions([
        {
            "trigger": {"task": spec.id, "step": "cut"},
            "prompt": "切配时请说慢一点。",
        }
    ])
    assert spec.id == "tomato-eggs"
    assert len(spec.steps) == 7
    assert "切配时请说慢一点" in context
    assert "看不清就询问" in context


def test_generic_task_session_progresses_from_spec():
    spec = TaskSpec.load(TASK)
    session = TaskSession(spec)
    session.voice("完成了")
    assert session.snapshot()["stepIndex"] == 1
    assert session.current_step.id == "wash"


def test_dynamic_task_validation_rejects_duplicate_steps():
    with pytest.raises(ValueError, match="duplicate"):
        TaskSpec.from_dict({
            "id": "dynamic-task", "title": "Dynamic", "version": "1", "role": "coach",
            "steps": [
                {"id": "same", "title": "A", "instruction": "Do A", "completion": "A done"},
                {"id": "same", "title": "B", "instruction": "Do B", "completion": "B done"},
            ],
        })


def test_research_replay_uses_active_task_step_ids():
    session = TaskSession(TaskSpec.load(TASK))
    observed = session.ingest(session.replay_observation("complete"))
    assert observed["observation"]["step"] == session.current_step.id
    assert observed["observation"]["source"] == "research-replay/deterministic"
    assert observed["decision"]["action"] == "ask"
