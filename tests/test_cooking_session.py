from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.realtime import StructuredPerceptionAdapter, TaskSession, TaskSpec


TASK = BUILTIN_SKILLS_ROOT / "kitchen-coach" / "tasks" / "fruit-salad.json"


def task_session():
    return TaskSession(TaskSpec.load(TASK))


def observation(step="clean", completed=False, confidence=0.9, hazards=None):
    return {
        "step": step,
        "completed": completed,
        "confidence": confidence,
        "summary": "test observation",
        "hazards": hazards or [],
        "source": "test",
    }


def test_step_completion_requires_user_confirmation():
    session = task_session()
    state = session.ingest(observation(completed=True))
    assert state["decision"]["action"] == "ask"
    assert state["stepIndex"] == 0

    state = session.apply_voice_intent("confirm-step", "完成了")
    assert state["decision"]["action"] == "guide"
    assert state["stepIndex"] == 1


def test_wrong_step_and_low_confidence_require_human_confirmation():
    session = task_session()
    wrong = session.ingest(observation(step="mix"))
    uncertain = session.ingest(observation(step="unknown", confidence=0.3))
    assert wrong["decision"]["action"] == "ask"
    assert uncertain["decision"]["action"] == "ask"


def test_voice_completion_questions_do_not_advance_and_rollback_is_explicit():
    session = task_session()
    assert session.apply_voice_intent(None, "还没完成")["stepIndex"] == 0
    assert session.apply_voice_intent(None, "下一步怎么做")["stepIndex"] == 0
    assert session.apply_voice_intent(None, "完成了吗")["stepIndex"] == 0
    assert session.apply_voice_intent("confirm-step", "完成了")["stepIndex"] == 1
    state = session.apply_voice_intent("rollback", "上一步")
    assert state["stepIndex"] == 0
    assert state["decision"]["reason"] == "用户请求回退并重新检查"


def test_hazard_preempts_normal_guidance():
    session = task_session()
    state = session.ingest(
        observation(
            hazards=[{
                "type": "hot_pan",
                "message": "请立即关火。",
                "confidence": 0.98,
            }]
        )
    )
    assert state["decision"]["action"] == "emergency"
    assert state["decision"]["message"] == "请立即关火。"


def test_confirmed_memory_hint_triggers_once_at_matching_step():
    session = task_session()
    record = {
        "id": "memory-1",
        "trigger": {"task": "水果沙拉", "step": "切配"},
        "prompt": "慢一点切。",
    }
    assert session.apply_memory_hint(record)["decision"]["reason"] == "开始任务"
    session.apply_voice_intent("confirm-step", "完成了")
    session.apply_voice_intent("confirm-step", "完成了")
    first = session.apply_memory_hint(record)
    session.apply_voice_intent("request-help", "怎么做")
    second = session.apply_memory_hint(record)
    assert first["decision"]["message"] == "慢一点切。"
    assert second["decision"]["message"] != "慢一点切。"


def test_manual_perception_is_explicit_about_no_inference():
    result = StructuredPerceptionAdapter("manual").analyze("not-an-image", "clean")
    assert result["step"] == "unknown"
    assert result["confidence"] == 0.0
    assert result["source"] == "camera/manual"
