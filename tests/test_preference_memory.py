import pytest

from egoassist.realtime import PreferenceMemory


def test_confirmed_feedback_is_reused_only_when_trigger_matches(tmp_path):
    path = tmp_path / "memory.json"
    memory = PreferenceMemory(path)
    learned = memory.propose_preference(
        task="oatmeal", step="rest", prompt="请提醒我先戴隔热手套。",
        evidence="用户在静置步骤请求更早的防烫提示",
    )
    assert learned["candidate"]["status"] == "pending"
    memory.resolve_candidate(True)

    restarted = PreferenceMemory(path)
    assert restarted.matching_record("oatmeal", "measure") is None
    assert restarted.matching_record("fruit-salad", "rest") is None
    assert restarted.matching_record("oatmeal", "rest")["prompt"] == "请提醒我先戴隔热手套。"


def test_unconfirmed_feedback_is_not_persisted(tmp_path):
    path = tmp_path / "memory.json"
    memory = PreferenceMemory(path)
    memory.propose_preference(task="fruit-salad", step="cut", prompt="慢一点。", evidence="explicit feedback")
    memory.resolve_candidate(False)

    assert PreferenceMemory(path).matching_record("fruit-salad", "cut") is None


def test_delete_prevents_future_intervention(tmp_path):
    memory = PreferenceMemory(tmp_path / "memory.json")
    memory.propose_preference(task="flat-pack-stool", step="tighten", prompt="先对角终拧。", evidence="explicit feedback")
    saved = memory.resolve_candidate(True)
    memory.delete_memory(saved["records"][0]["id"])

    assert memory.matching_record("flat-pack-stool", "tighten") is None
    assert memory.snapshot()["records"] == []


def test_generic_preference_uses_task_and_step(tmp_path):
    memory = PreferenceMemory(tmp_path / "memory.json")
    pending = memory.propose_preference(
        task="assemble_filter",
        step="insert_gasket",
        prompt="先确认密封圈平整。",
        evidence="用户请求在此步骤提前提醒",
    )
    assert pending["candidate"]["trigger"] == {
        "task": "assemble_filter",
        "step": "insert_gasket",
    }
    memory.resolve_candidate(True)
    assert memory.matching_record("assemble_filter", "insert_gasket")["prompt"] == "先确认密封圈平整。"


def test_pending_candidates_are_isolated_by_experiment_case(tmp_path):
    memory = PreferenceMemory(tmp_path / "memory.json")
    first = memory.propose_preference(
        task="task-a",
        step="step-a",
        prompt="案例 A 的偏好",
        evidence="case-a evidence",
        candidate_scope="case-a",
    )

    assert first["candidate"]["preference"] == "案例 A 的偏好"
    assert memory.snapshot(candidate_scope="case-b")["candidate"] is None
    with pytest.raises(ValueError, match="no pending memory candidate"):
        memory.resolve_candidate(True, candidate_scope="case-b")

    memory.resolve_candidate(True, candidate_scope="case-a")
    assert memory.matching_record("task-a", "step-a")["preference"] == "案例 A 的偏好"


def test_pending_candidate_survives_restart_and_remains_case_scoped(tmp_path):
    path = tmp_path / "memory.json"
    PreferenceMemory(path).propose_preference(
        task="task-a",
        step="step-a",
        prompt="persist me",
        evidence="explicit request",
        candidate_scope="case-a",
    )

    restarted = PreferenceMemory(path)
    assert restarted.snapshot(candidate_scope="case-a")["candidate"]["prompt"] == "persist me"
    assert restarted.snapshot(candidate_scope="case-b")["candidate"] is None
    restarted.resolve_candidate(False, candidate_scope="case-a")
    assert PreferenceMemory(path).snapshot(candidate_scope="case-a")["candidate"] is None
