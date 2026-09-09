import threading
from collections import deque

from egoassist.realtime import EventLedger, PreferenceMemory, TaskSession, TaskSpec, TurnTakingPolicy
from egoassist.realtime.omni import OmniBrowserBridge, _ResponseRequest


def simple_task():
    return TaskSpec.from_dict({
        "id": "research-task", "title": "Research", "version": "1", "role": "assistant",
        "steps": [
            {"id": "a", "title": "A", "instruction": "do A", "completion": "A done"},
            {"id": "b", "title": "B", "instruction": "do B", "completion": "B done"},
        ],
    })


def test_event_ledger_keeps_causal_metadata():
    ledger = EventLedger(session_id="session-test")
    parent = ledger.append("media.video", "camera", {"frameId": "f1"})
    child = ledger.append("perception.observation", "adapter", {}, parent_event_ids=[parent["eventId"]])
    assert child["sessionId"] == "session-test"
    assert child["parentEventIds"] == [parent["eventId"]]
    assert child["timestampMonotonic"] >= parent["timestampMonotonic"]


def test_exact_memory_retrieval_exposes_rejection_reasons(tmp_path):
    memory = PreferenceMemory(tmp_path / "memory.json")
    memory.propose_preference(task="research-task", step="a", prompt="说慢一点", evidence="quote")
    memory.resolve_candidate(True)
    result = memory.retrieve("research-task", "b", emit=False)
    assert result["method"] == "timeline-exact-task-trigger-v1"
    assert result["selected"] is None
    assert result["candidates"][0]["rejectionReasons"] == ["checkpoint-mismatch"]


def test_unexpected_path_is_hypothesis_not_automatic_progress():
    session = TaskSession(simple_task())
    state = session.ingest({
        "step": "b", "completed": True, "confidence": 0.9,
        "summary": "B may be complete", "hazards": [], "source": "test-adapter",
    })
    assert state["checkpointLedger"]["b"]["status"] == "hypothesized"
    assert state["checkpointLedger"]["a"]["status"] == "active"
    assert state["progress"] == 0


class Conversation:
    def __init__(self):
        self.cancelled = 0

    def cancel_response(self):
        self.cancelled += 1


def speech_bridge(policy):
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._response_lock = threading.Lock()
    bridge._response_pending = False
    bridge._active_response_ids = {"r1"}
    bridge._active_response_requests = {"r1": _ResponseRequest("text", 50, 1, policy, "test", 0.0)}
    bridge._queued_requests = deque()
    bridge._inflight_response = None
    bridge._cancelled_response_ids = set()
    bridge._speech_started_response_ids = set()
    bridge._early_terminal_response_ids = set()
    bridge._cancel_when_created = False
    bridge._barge_in_pending = False
    bridge._trace_sink = None
    bridge._assistant_transcript_buffers = {}
    bridge.conversation = Conversation()
    bridge.playback_events = []
    bridge._schedule = bridge.playback_events.append
    return bridge


def test_speech_policies_distinguish_immediate_clause_and_protected():
    immediate = speech_bridge("immediate")
    assert immediate.handle_user_speech_started() == "cancelled"
    assert immediate.conversation.cancelled == 1
    assert immediate.playback_events[0]["type"] == "playback_stop"

    clause = speech_bridge("finish-clause")
    assert clause.handle_user_speech_started() == "deferred"
    assert clause.conversation.cancelled == 0
    assert clause.playback_events[0]["type"] == "playback_stop"
    clause._schedule = lambda payload: None
    clause.note_assistant_transcript("这一句结束。")
    assert clause.conversation.cancelled == 1

    protected = speech_bridge("protected")
    assert protected.handle_user_speech_started() == "protected"
    assert protected.conversation.cancelled == 0
    assert protected.playback_events[0]["type"] == "playback_stop"


def test_turn_taking_policy_is_skill_configurable_and_decision_overridable():
    policy = TurnTakingPolicy.from_manifest({"turn_taking": {
        "finish_clause_actions": ["guide"], "protected_actions": ["ask"],
        "max_protection_ms": 1200,
    }})
    assert policy.select({"action": "guide"})["policy"] == "finish-clause"
    selected = policy.select({"action": "ask", "speechPolicy": "immediate"})
    assert selected["policy"] == "immediate"
    assert selected["basis"] == "decision-explicit"
    assert policy.max_protection_ms == 1200
