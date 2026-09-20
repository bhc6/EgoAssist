import asyncio
import json
from types import SimpleNamespace

from egoassist.memory import TimelineMemory
from egoassist.memory.commit import MemoryCommitService
from egoassist.memory.temporal import ClockContext
from egoassist.realtime import SemanticTurnRouter


def router_for(atom, calls):
    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, output=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps({"v": 1, "memoryAtoms": [atom]})))
        ]))
    return SemanticTurnRouter("test-key", generation_call=generate)


def correction():
    return {"kind": "state", "text": "The spare key is in the study.", "subject": "spare key",
            "predicate": "location", "value": "study", "scope": "participant",
            "consent": "explicit", "replaces": "r1"}


def test_text_correction_gets_scoped_cards_without_camera():
    calls = []
    router = router_for(correction(), calls)
    async def run():
        router.set_turn_context(clock_context=None, memory_draft=None,
            memory_candidates=[{"id": "actual-card", "referenceId": "r1", "kind": "state"}],
            memory_vocabulary=[{"subject": "spare key", "predicate": "location"}],
            active_task={"task": {"id": "commute-check"}, "paused": False})
        return await router.classify("Correct the remembered location: the spare key is in the study.", [])
    result = asyncio.run(run())
    assert result["componentErrors"] == []
    assert result["memoryAtoms"][0]["replaces"] == "actual-card"
    prompt = calls[0]["messages"][0]["content"]
    assert '"memoryCandidates"' in prompt and '"actual-card"' in prompt
    assert '"activeTask"' in prompt and '"commute-check"' in prompt
    assert '"memoryVocabulary"' in prompt and '"location"' in prompt


def test_invalid_replacement_keeps_explicit_request_but_cannot_write(tmp_path):
    result = asyncio.run(router_for(correction(), []).classify("Correct my remembered spare key location.", []))
    assert result["componentErrors"]
    assert result["memoryAtoms"] == []
    assert result["memoryRequestRejected"] is True
    timeline = TimelineMemory(tmp_path / "memory.sqlite3")
    service = MemoryCommitService(lambda *a, **k: None, {}, None, None, timeline)
    write, requested, _, _ = service.commit(
        active_episodic=None, active_runtime=SimpleNamespace(skill=None, task=None),
        clock_context=ClockContext.current("UTC"), episodic_settings={"episodic_enabled": True},
        harness=None, preference_enabled=True, semantic_decision=result,
        semantic_method=result["method"], transcript="Correct my remembered spare key location.",
        transcript_event={"eventId": "source"}, turn_id="turn", user_scope="u")
    assert requested is True
    assert write["status"] == "queued"
    assert write["committedCardIds"] == []
    assert timeline.cards(user_id="u") == []


def test_malformed_memory_does_not_drop_valid_task_control():
    def generate(**kwargs):
        return SimpleNamespace(status_code=200, output=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(
                {"v": 1, "taskControl": "pause-task", "memoryAtoms": 42})))
        ]))
    result = asyncio.run(SemanticTurnRouter("test", generation_call=generate).classify("Pause my task.", []))
    assert result["status"] == "classified"
    assert result["taskControl"] == "pause-task"
    assert result["componentErrors"]


def test_observed_participant_memory_rejected_without_muting_dialogue():
    atom = {"kind": "state", "text": "My usual keys are in my bag.", "subject": "usual keys",
            "predicate": "location", "value": "bag", "scope": "participant", "consent": "observed"}
    result = asyncio.run(router_for(atom, []).classify(atom["text"], []))
    assert result["status"] == "classified"
    assert result["interaction"] is None
    assert result["memoryAtoms"] == []
    assert result["memoryKind"] is None
    assert result["componentErrors"][0]["component"] == "memoryAtoms"
    assert result["memoryRequestRejected"] is False


def test_everyday_departure_request_selects_checklist_without_guessing_trip():
    from egoassist import BUILTIN_SKILLS_ROOT
    from egoassist.realtime.skills import SkillRegistry
    registry = SkillRegistry(BUILTIN_SKILLS_ROOT)
    route = registry.route("我快迟到了，帮我做个出门检查吧，今天要带钥匙、手机和门禁卡。")
    assert route["selectedSkill"] == "packing-coach"
    assert route["selectedTask"] == "commute-check"
    assert registry.route("帮我整理行李")['selectedTask'] is None


def test_router_task_context_exposes_current_completion_criterion():
    from egoassist.realtime.router_context import project_task
    task = {'task': {'id': 'check'}, 'currentCheckpointId': 'gather', 'paused': False, 'completed': False,
            'steps': [{'id': 'gather', 'title': 'Find items', 'instruction': 'Find every agreed item.',
                       'completion': 'All items found, not a partial list.', 'status': 'active'},
                      {'id': 'carry', 'completion': 'All items carried.'}], 'trace': ['private-noise']}
    result = project_task(task)
    assert result['currentStep']['completion'] == 'All items found, not a partial list.'
    assert 'trace' not in result
    assert project_task(None) is None


def test_task_review_rejects_partial_confirmation_and_preserves_reply():
    responses = iter([{'v': 1, 'taskControl': 'confirm-step'},
                      {'taskControl': 'none', 'taskDirected': True}])
    def generate(**kwargs):
        return SimpleNamespace(status_code=200, output=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(responses))))]))
    router = SemanticTurnRouter('test', generation_call=generate)
    async def run():
        router.set_turn_context(clock_context=None, memory_draft=None,
                                active_task={'currentStep': {'completion': 'All items found'}})
        return await router.classify('Two items found, one still missing.', [])
    result = asyncio.run(run())
    assert result['status'] == 'classified'
    assert result['taskControl'] is None
    assert result['interaction'] is None
    assert 'taskControlCheck' in result['requestMetrics']


def test_task_review_routes_departure_pause_to_task_not_answer():
    responses = iter([{'v': 1, 'interaction': {'audience': 'assistant', 'action': 'pause',
                                             'output': 'silent', 'targetTurnId': 'old'}},
                      {'taskControl': 'pause-task', 'taskDirected': True}])
    def generate(**kwargs):
        return SimpleNamespace(status_code=200, output=SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(responses))))]))
    router = SemanticTurnRouter('test', generation_call=generate)
    async def run():
        router.set_turn_context(clock_context=None, memory_draft=None,
                                active_task={'currentStep': {'completion': 'All items carried'}})
        return await router.classify('Someone is at the door, I will continue after I return.', [])
    result = asyncio.run(run())
    assert result['taskControl'] == 'pause-task'
    assert result['interaction'] == {'audience': 'assistant', 'action': 'reply', 'output': 'speech'}
