import json
import threading
import asyncio
import pytest
from collections import deque

from egoassist.realtime import ContextHarness, ContextSettings, SessionJournal
from egoassist.realtime.omni import OmniBrowserBridge


class Modalities:
    AUDIO = "audio"
    TEXT = "text"


class Conversation:
    def __init__(self):
        self.items = []
        self.responses = []
        self.sessions = []
        self.cancelled = 0

    def create_item(self, item):
        self.items.append(item)

    def create_response(self, **kwargs):
        self.responses.append(kwargs)

    def update_session(self, **kwargs):
        self.sessions.append(kwargs)

    def cancel_response(self):
        self.cancelled += 1


def make_bridge(harness):
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._context_harness = harness
    bridge._provider_tools = []
    bridge._tool_handler = None
    bridge._pending_tool_calls = {}
    bridge._dialogue_generation = 3
    bridge._trace_sink = None
    bridge.connected = True
    bridge.closed = False
    bridge.voice = "test"
    bridge.instructions = "policy"
    bridge.modalities = Modalities
    bridge.conversation = Conversation()
    bridge._response_lock = threading.Lock()
    bridge._response_pending = False
    bridge._active_response_ids = set()
    bridge._cancelled_response_ids = set()
    bridge._speech_started_response_ids = set()
    bridge._early_terminal_response_ids = set()
    bridge._active_response_requests = {}
    bridge._queued_requests = deque()
    bridge._inflight_response = None
    bridge._response_sequence = 0
    bridge._cancel_when_created = False
    bridge._barge_in_pending = False
    bridge._response_text = {}
    bridge._output_channels_seen = set()
    bridge._suppressed_output_channels_seen = set()
    bridge._assistant_transcript_buffers = {}
    bridge._last_assistant_utterance = ""
    bridge._last_assistant_utterance_at = 0.0
    bridge._replayed_compaction_id = None
    bridge._schedule = lambda payload: None
    return bridge


def harness_at(path):
    return ContextHarness(
        SessionJournal(path, session_id="bridge-session"),
        ContextSettings(context_window=2048, reserve_tokens=256, keep_recent_tokens=512, max_summary_tokens=256),
    )


@pytest.mark.parametrize("mode", ["batch", "budget", "identity-reuse", "timeout", "cancel", "close-executing", "receipted", "no-tools", "proactive"])
def test_async_tool_loop_lifecycle(tmp_path, monkeypatch, mode):
    if mode == "budget":
        monkeypatch.setenv("EGOASSIST_TOOL_MAX_CALLS", "1")
    async def run():
        harness = harness_at(tmp_path / (mode + ".jsonl"))
        bridge = make_bridge(harness)
        bridge.loop = asyncio.get_running_loop()
        bridge._schedule_queued_dispatch = lambda: None
        started, release = asyncio.Event(), asyncio.Event()
        executions, selections = [], []

        class Selector:
            model = "test-selector"

            async def select(self, *args, history=()):
                selections.append(args)
                started.set()
                if mode == "timeout":
                    raise TimeoutError()
                if mode == "cancel":
                    await release.wait()
                if history:
                    if mode == "identity-reuse":
                        return {"tool_calls": [{"id": "call-1", "function": {"name": "action", "arguments": '{"index":9}'}}]}
                    return {"content": "completed"}
                return {"tool_calls": [{"id": "call-" + str(i), "function": {"name": "action", "arguments": json.dumps({"index": i})}}
                                       for i in (1, 2, 1)]}

        def execute(name, arguments, confirmed):
            executions.append(confirmed)
            if mode == "close-executing":
                bridge.loop.call_soon_threadsafe(started.set)
                asyncio.run_coroutine_threadsafe(release.wait(), bridge.loop).result(timeout=5)
            return {"status": "completed"}

        bridge.tool_model = Selector()
        bridge.set_tools([] if mode == "no-tools" else [{"name": "action", "arguments": {"type": "object"}}], execute)
        bridge.set_tool_result_handler(lambda call_id, *args: {"resultId": "result-" + call_id})
        if mode == "receipted":
            harness.record_tool_result("already", "action", {"status": "completed"}, metadata={"origin": {"turnId": "turn"}})
        bridge.request_response("Perform the action", origin={"turnId": "turn"},
                                purpose="proactive-utterance" if mode == "proactive" else "conversation")
        if mode in {"receipted", "no-tools", "proactive"}:
            assert not selections and len(bridge.conversation.responses) == 1
            return
        await asyncio.wait_for(started.wait(), 2)
        if mode == "cancel":
            bridge.cancel_response()
        if mode == "close-executing":
            while not executions:
                await asyncio.sleep(0.001)
            bridge.closed = True
            bridge.connected = False
            close_task = asyncio.create_task(bridge._tool_runner.close())
            await asyncio.sleep(0)
            assert not close_task.done()
            release.set()
            await close_task
        else:
            while bridge._tool_runner.tasks:
                await asyncio.sleep(0.001)
        results = [e for e in harness.journal.entries() if e["type"] == "tool_result"]
        if mode in {"batch", "identity-reuse"}:
            assert executions == [True, True]
            assert len(results) == 2 and len(bridge.conversation.responses) == 1
            assert bridge._inflight_response.origin["resultIds"] == ["result-call-1", "result-call-2"]
            deliveries = []
            bridge.set_result_delivery_handler(lambda *args: deliveries.append(args))
            bridge._return_result_to_inbox(bridge._inflight_response, "test")
            assert deliveries == [("result-call-1", "pending"), ("result-call-2", "pending")]
            if mode == "identity-reuse":
                assert any(e["type"] == "tool.selection.finished" and e["status"] == "failed" for e in harness.journal.entries())
        elif mode == "budget":
            assert len(executions) == len(results) == 1
            assert any(e["type"] == "tool.deferred" for e in harness.journal.entries())
            assert any(e["type"] == "tool.selection.finished" and e["status"] == "budget-exhausted" for e in harness.journal.entries())
        elif mode == "close-executing":
            assert len(results) == 1 and not bridge.conversation.responses
            assert not bridge.conversation.items
        else:
            assert not executions and not results
            assert len(bridge.conversation.responses) == (1 if mode == "timeout" else 0)
            if mode == "cancel":
                assert not bridge._response_pending

    asyncio.run(run())


def test_incremental_tool_activity_retains_unknown_outcome_and_selection(tmp_path):
    harness = harness_at(tmp_path / "projection.jsonl")
    journal = harness.journal
    journal.append("tool.request", {"model": "actual-model", "messages": [{"content": "input"}],
                                    "origin": {"selectionId": "select-1"}})
    assert journal.tool_activity()[0]["status"] == "selecting"
    journal.append("tool.selection", {"model": "actual-model", "latencyMs": 42,
                                      "origin": {"selectionId": "select-1"}, "message": {"content": "clarify"}})
    harness.record_tool_call("call", "action", {})
    harness.record_tool_result("call", "action", {"status": "interrupted"}, is_error=True)
    rows = journal.tool_activity()
    assert rows[0]["latencyMs"] == 42 and rows[1]["status"] == "interrupted"
    assert SessionJournal(journal.path).tool_activity() == rows
    assert journal.tool_activity() == rows


def test_automatic_tools_execute_once_without_confirmation(tmp_path):
    harness = harness_at(tmp_path / "automatic.jsonl")
    bridge = make_bridge(harness)
    bridge.auto_approve_tools = True
    calls, messages = [], []
    bridge._schedule = messages.append
    bridge.set_tools([{"name": "action", "arguments": {"type": "object"}}],
                     lambda name, arguments, confirmed: calls.append(confirmed) or {"status": "completed"})
    event = {"call_id": "auto-1", "name": "action", "arguments": "{}"}
    bridge._handle_tool_call_event(event)
    bridge._handle_tool_call_event(event)
    assert calls == [True]
    assert not any(m["type"] == "tool_confirmation_required" for m in messages)
    assert [e["type"] for e in harness.journal.entries()] == ["tool_call", "tool_execution", "tool_result"]
    assert messages[-1]["status"] == "completed"


def test_invalid_json_returns_error_instead_of_leaving_tool_unanswered(tmp_path):
    bridge = make_bridge(harness_at(tmp_path / "invalid.jsonl"))
    bridge._handle_tool_call_event({"call_id": "invalid-1", "name": "action", "arguments": "[1]"})
    result = json.loads(bridge.conversation.items[-1]["output"])
    assert result["status"] == "error"
    assert "JSON object" in result["error"]


def test_recovery_preserves_source_and_does_not_reexecute_unknown_effect(tmp_path):
    harness = harness_at(tmp_path / "crash.jsonl")
    origin = {"turnId": "original-question", "tools": [{"name": "action"}]}
    harness.record_tool_call("pending", "action", {}, metadata={"origin": origin})
    harness.journal.append("tool_confirmation", {"callId": "pending", "name": "action", "arguments": {}, "status": "pending"})
    harness.record_tool_call("executing", "action", {}, metadata={"origin": origin})
    harness.journal.append("tool_execution", {"callId": "executing", "accepted": True, "origin": origin})
    bridge = make_bridge(harness)
    bridge.set_context_harness(harness)
    assert bridge._pending_tool_calls["pending"]["origin"] == origin
    assert "executing" not in bridge._pending_tool_calls
    result = harness.journal.entries()[-1]
    assert result["result"]["status"] == "interrupted" and result["isError"]
    assert result["metadata"]["origin"] == origin


def test_duplicate_confirmation_is_idempotent_and_bool_is_strict(tmp_path):
    bridge = make_bridge(harness_at(tmp_path / "duplicate-confirm.jsonl"))
    calls = []
    bridge.set_tools([], lambda *args: calls.append(args) or {"status": "completed"})
    bridge._pending_tool_calls["one"] = {"name": "action", "arguments": {}, "generation": 3}
    with pytest.raises(ValueError, match="boolean"):
        bridge.confirm_tool("one", "false")
    assert bridge.confirm_tool("one", True)
    assert bridge.confirm_tool("one", True)
    assert len(calls) == 1


def test_speech_onset_stops_device_playback_even_after_cloud_finished(tmp_path):
    bridge = make_bridge(harness_at(tmp_path / "device-stop.jsonl"))
    messages = []
    bridge._schedule = messages.append
    assert bridge.handle_user_speech_started() == "idle"
    assert messages[0]["type"] == "playback_stop"
    assert messages[0]["responseIds"] == []
    assert bridge.conversation.cancelled == 0


def test_provider_projection_replays_durable_context(tmp_path):
    harness = harness_at(tmp_path / "session.jsonl")
    harness.record_user("remember this", source="test", turn_id="one")
    harness.record_assistant("remembered", response_id="response-1")
    bridge = make_bridge(harness)

    count = bridge._replay_context(bridge.conversation)

    assert count == 2
    assert [item["role"] for item in bridge.conversation.items] == ["user", "assistant"]
    assert bridge.conversation.items[0]["content"][0]["type"] == "input_text"


def test_startup_tool_recovery_never_copies_unbounded_unrelated_events(tmp_path, monkeypatch):
    harness = harness_at(tmp_path / "bounded-recovery.jsonl")
    harness.record_tool_call("pending", "action", {})
    harness.journal.append("tool_confirmation", {
        "callId": "pending", "name": "action", "arguments": {}, "status": "pending",
    })
    for _ in range(520):
        harness.journal.append("runtime_event", {"payload": {"unrelated": "observation"}})
    entries = harness.journal.entries

    def bounded_entries(*, types=None, tail=None):
        assert types is not None or tail is not None
        return entries(types=types, tail=tail)

    monkeypatch.setattr(harness.journal, "entries", bounded_entries)
    bridge = make_bridge(harness)
    bridge.set_context_harness(harness)
    assert bridge._pending_tool_calls["pending"]["name"] == "action"
    assert "pending" not in getattr(bridge, "_tool_call_signatures", {})


def test_assistant_completion_is_committed_once_by_response_id(tmp_path):
    harness = harness_at(tmp_path / "assistant.jsonl")
    bridge = make_bridge(harness)
    bridge.request_response("answer", purpose="conversation")
    bridge._response_created({"response": {"id": "response-2"}})
    payload = {"response_id": "response-2", "transcript": "durable answer"}

    bridge.route_provider_output("response.audio_transcript.done", payload)
    bridge.route_provider_output("response.audio_transcript.done", payload)
    bridge._response_finished({"type": "response.done", "response": {"id": "response-2"}})

    messages = [entry for entry in harness.journal.entries() if entry["type"] == "message"]
    assert len(messages) == 1
    assert messages[0]["message"] == {"role": "assistant", "content": "durable answer"}


def test_completed_transcript_buffer_is_not_recommitted_at_response_end(tmp_path):
    harness = harness_at(tmp_path / "assistant-buffer.jsonl")
    bridge = make_bridge(harness)
    bridge.request_response("answer", purpose="conversation")
    bridge._response_created({"response": {"id": "response-buffer"}})
    bridge.route_provider_output("response.audio_transcript.delta", {
        "response_id": "response-buffer", "delta": "durable ",
    })
    bridge.route_provider_output("response.audio_transcript.done", {
        "response_id": "response-buffer", "transcript": "durable answer",
    })

    bridge._response_finished({"type": "response.done", "response": {"id": "response-buffer"}})

    messages = [entry for entry in harness.journal.entries() if entry["type"] == "message"]
    assert len(messages) == 1
    assert messages[0]["message"]["content"] == "durable answer"


def test_skill_exit_sends_empty_tools_and_general_context_to_provider(tmp_path):
    bridge = make_bridge(harness_at(tmp_path / "exit.jsonl"))
    bridge.set_tools([{
        "name": "show_reference", "description": "Reference",
        "arguments": {"type": "object", "properties": {}},
    }], lambda *args: {})
    assert bridge.conversation.sessions[-1]["tools"]
    bridge.set_tools([], lambda *args: {})
    bridge.apply_context_projection("General assistant; no active Skill.", {})
    assert bridge.conversation.sessions[-1]["tools"] == []
    assert bridge.conversation.sessions[-1]["instructions"] == "General assistant; no active Skill."


def test_declared_tool_executes_and_commits_result_before_continuation(tmp_path):
    harness = harness_at(tmp_path / "tool.jsonl")
    bridge = make_bridge(harness)
    committed = []
    bridge.set_tool_result_handler(
        lambda call_id, name, result, is_error, entry: committed.append(
            (call_id, name, result, is_error, entry["type"]),
        ),
    )
    bridge.set_tools([
        {
            "name": "timer",
            "description": "Set a timer",
            "requires_confirmation": False,
            "arguments": {
                "type": "object",
                "properties": {"seconds": {"type": "integer"}},
                "required": ["seconds"],
            },
        }
    ], lambda name, arguments, confirmed: {"status": "scheduled", "seconds": arguments["seconds"]})

    bridge._handle_tool_call_event({
        "type": "response.function_call_arguments.done",
        "call_id": "call-1",
        "name": "timer",
        "arguments": '{"seconds":5}',
        "response_id": "response-tool",
    })

    assert bridge.conversation.sessions[-1]["tools"][0]["function"]["name"] == "timer"
    assert "tool_choice" not in bridge.conversation.sessions[-1]
    assert bridge.conversation.items[-1]["type"] == "function_call_output"
    assert json.loads(bridge.conversation.items[-1]["output"])["status"] == "scheduled"
    assert [entry["type"] for entry in harness.journal.entries()] == ["tool_call", "tool_result"]
    assert committed == [("call-1", "timer", {"status": "scheduled", "seconds": 5}, False, "tool_result")]
    assert bridge.conversation.responses[-1]["instructions"].startswith("Continue only the source intent")
    assert "reports success" in bridge.conversation.responses[-1]["instructions"]


def test_tool_call_keeps_source_turn_intent_and_skill_contract(tmp_path):
    harness = harness_at(tmp_path / "origin-tool.jsonl")
    bridge = make_bridge(harness)
    origins = []
    bridge.set_tools([{
        "name": "shared_name", "description": "Source skill behavior",
        "requires_confirmation": False,
        "arguments": {"type": "object", "properties": {}},
    }], lambda name, arguments, confirmed, origin: origins.append(origin) or {"status": "accepted"})
    origin = {
        "turnId": "turn-old", "intentId": "intent-old",
        "skill": {"name": "source-skill", "version": "1"},
        "tools": [{"name": "shared_name"}],
    }
    bridge.request_response("source response", origin=origin)
    bridge._response_created({"response": {"id": "response-origin"}})

    bridge._handle_tool_call_event({
        "call_id": "call-origin", "name": "shared_name", "arguments": "{}",
        "response_id": "response-origin",
    })

    assert origins == [origin]
    tool_call = next(item for item in harness.journal.entries() if item["type"] == "tool_call")
    assert tool_call["metadata"]["origin"] == origin
    snapshot = bridge.scheduler_snapshot()
    queued = snapshot["queued"] or []
    pending = queued[0] if queued else snapshot["inflight"]
    assert pending["origin"] == origin


def test_result_delivery_handler_tracks_response_terminal_state(tmp_path):
    bridge = make_bridge(harness_at(tmp_path / "delivery.jsonl"))
    delivered = []
    bridge.set_result_delivery_handler(lambda result_id, status: delivered.append((result_id, status)))

    bridge.request_response("deliver first", origin={"resultId": "result-1"})
    bridge._response_created({"response": {"id": "response-1"}})
    bridge._response_finished({"type": "response.done", "response": {"id": "response-1"}})

    bridge.request_response("deliver second", origin={"resultId": "result-2"})
    bridge._response_created({"response": {"id": "response-2"}})
    bridge._response_finished({"type": "response.cancelled", "response": {"id": "response-2"}})

    assert delivered == [("result-1", "delivered"), ("result-2", "pending")]


@pytest.mark.parametrize("provider_status", ["cancelled", "failed", "incomplete"])
def test_provider_done_with_failure_never_claims_completed_delivery(tmp_path, provider_status):
    harness = harness_at(tmp_path / "nested-failure.jsonl")
    bridge = make_bridge(harness)
    events = []
    lifecycle = []
    bridge._schedule = events.append
    bridge.set_response_lifecycle_handler(lambda origin, status: lifecycle.append((origin, status)))
    origin = {"turnId": "question-a", "generation": 7, "tools": [{"name": "private-contract"}]}
    bridge.request_response("answer", origin=origin)
    bridge._response_created({"response": {"id": "answer-a"}})
    bridge.route_provider_output("response.audio.delta", {"response_id": "answer-a", "delta": "AAAA"})
    bridge.route_provider_output("response.audio_transcript.done", {
        "response_id": "answer-a", "transcript": "partial answer",
    })
    bridge._response_finished({"type": "response.done", "response": {"id": "answer-a", "status": provider_status}})

    assert lifecycle == [({**origin, "responseId": "answer-a"}, "suspended")]
    assert events[-1]["status"] == "interrupted"
    assert all(event["responseId"] == "answer-a" and event["origin"] == {
        "turnId": "question-a", "generation": 7,
    } for event in events)
    messages = [entry for entry in harness.journal.entries() if entry["type"] == "message"]
    assert len(messages) == 1 and messages[0]["status"] == "interrupted"


def test_replaced_queued_result_is_returned_to_inbox(tmp_path):
    bridge = make_bridge(harness_at(tmp_path / "requeue.jsonl"))
    requeued = []
    bridge.set_result_delivery_handler(lambda result_id, status: requeued.append((result_id, status)))
    bridge.request_response("busy foreground")
    bridge.request_response("background result", priority=40, origin={"resultId": "result-late"})
    bridge.request_response("new foreground", priority=100, interrupt=True)
    assert requeued == [("result-late", "pending")]


def test_failed_tool_result_cannot_authorize_success_claim(tmp_path):
    harness = harness_at(tmp_path / "failed-tool.jsonl")
    bridge = make_bridge(harness)
    bridge.set_tools([{
        "name": "timer", "description": "Set a timer",
        "arguments": {"type": "object", "properties": {}},
    }], lambda name, arguments, confirmed: {"status": "unsupported", "reason": "no scheduler"})

    bridge._handle_tool_call_event({
        "call_id": "call-no-timer", "name": "timer", "arguments": "{}",
    })

    instruction = bridge.conversation.responses[-1]["instructions"]
    assert "does not report success" in instruction
    assert "do not claim the action was completed" in instruction


def test_tool_effect_memory_failure_does_not_rewrite_committed_tool_outcome(tmp_path):
    harness = harness_at(tmp_path / "tool-effect-failure.jsonl")
    bridge = make_bridge(harness)
    bridge.set_tool_result_handler(
        lambda *args: (_ for _ in ()).throw(RuntimeError("memory unavailable")),
    )
    bridge.set_tools([{
        "name": "helper", "description": "Local helper",
        "arguments": {"type": "object", "properties": {}},
    }], lambda name, arguments, confirmed: {"status": "accepted", "message": "queued locally"})

    bridge._handle_tool_call_event({
        "call_id": "call-memory-failure", "name": "helper", "arguments": "{}",
    })

    result_entries = [entry for entry in harness.journal.entries() if entry["type"] == "tool_result"]
    assert len(result_entries) == 1
    assert result_entries[0]["result"]["status"] == "accepted"
    assert json.loads(bridge.conversation.items[-1]["output"])["status"] == "accepted"
    assert "reports success" in bridge.conversation.responses[-1]["instructions"]


def test_qwen_output_item_tool_event_executes_declared_tool(tmp_path):
    harness = harness_at(tmp_path / "qwen-tool.jsonl")
    bridge = make_bridge(harness)
    bridge.set_tools([{
        "name": "timer",
        "description": "Set a timer",
        "requires_confirmation": False,
        "arguments": {"type": "object", "properties": {"seconds": {"type": "integer"}}},
    }], lambda name, arguments, confirmed: {"status": "scheduled", "seconds": arguments["seconds"]})

    assert bridge._handle_output_item_event({
        "type": "response.output_item.done",
        "response_id": "response-tool",
        "item": {
            "type": "function_call",
            "call_id": "call-qwen",
            "name": "timer",
            "arguments": '{"seconds":7}',
        },
    }) is True

    assert json.loads(bridge.conversation.items[-1]["output"])["seconds"] == 7
    assert [entry["type"] for entry in harness.journal.entries()] == ["tool_call", "tool_result"]


def test_duplicate_tool_callbacks_execute_once(tmp_path):
    harness = harness_at(tmp_path / "duplicate-tool.jsonl")
    bridge = make_bridge(harness)
    executions = []
    bridge.set_tools([{
        "name": "timer", "description": "Set a timer",
        "arguments": {"type": "object", "properties": {"seconds": {"type": "integer"}}},
    }], lambda name, arguments, confirmed: executions.append(arguments) or {"status": "scheduled"})
    event = {
        "call_id": "same-call", "name": "timer", "arguments": '{"seconds":5}',
        "response_id": "response-tool",
    }

    bridge._handle_tool_call_event(event)
    bridge._handle_output_item_event({
        "response_id": "response-tool",
        "item": {"type": "function_call", **event},
    })

    assert executions == [{"seconds": 5}]
    assert len(bridge.conversation.items) == 1
    assert len(bridge.conversation.responses) == 1
    assert [entry["type"] for entry in harness.journal.entries()] == ["tool_call", "tool_result"]


def test_conflicting_tool_call_id_reuse_fails_closed(tmp_path):
    harness = harness_at(tmp_path / "conflicting-tool.jsonl")
    bridge = make_bridge(harness)
    executions = []
    emitted = []
    bridge._schedule = emitted.append
    bridge.set_tools([{
        "name": "timer", "description": "Set a timer",
        "arguments": {"type": "object", "properties": {"seconds": {"type": "integer"}}},
    }], lambda name, arguments, confirmed: executions.append(arguments) or {"status": "scheduled"})

    bridge._handle_tool_call_event({
        "call_id": "reused", "name": "timer", "arguments": '{"seconds":5}',
    })
    bridge._handle_tool_call_event({
        "call_id": "reused", "name": "timer", "arguments": '{"seconds":9}',
    })

    assert executions == [{"seconds": 5}]
    assert emitted[-1]["type"] == "error"
    assert len([item for item in harness.journal.entries() if item["type"] == "tool_result"]) == 1


def test_concurrent_duplicate_tool_callbacks_claim_once(tmp_path):
    harness = harness_at(tmp_path / "concurrent-tool.jsonl")
    bridge = make_bridge(harness)
    executions = []
    barrier = threading.Barrier(8)
    errors = []
    bridge.set_tools([{
        "name": "timer", "description": "Set a timer",
        "arguments": {"type": "object", "properties": {"seconds": {"type": "integer"}}},
    }], lambda name, arguments, confirmed: executions.append(arguments) or {"status": "scheduled"})
    event = {"call_id": "concurrent", "name": "timer", "arguments": '{"seconds":3}'}

    def invoke():
        try:
            barrier.wait()
            bridge._handle_tool_call_event(event)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=invoke) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert executions == [{"seconds": 3}]
    assert len(bridge.conversation.items) == 1
    assert len(bridge.conversation.responses) == 1


def test_confirmed_tool_waits_for_explicit_approval(tmp_path):
    harness = harness_at(tmp_path / "confirm.jsonl")
    bridge = make_bridge(harness)
    emitted = []
    bridge._schedule = emitted.append

    def handler(name, arguments, confirmed):
        if not confirmed:
            return {"status": "confirmation_required", "tool": name, "arguments": arguments}
        return {"status": "accepted"}

    bridge.set_tools([{
        "name": "human_help",
        "description": "Ask a helper",
        "requires_confirmation": True,
        "arguments": {"type": "object", "properties": {}},
    }], handler)
    bridge._handle_tool_call_event({
        "call_id": "call-confirm", "name": "human_help", "arguments": "{}",
    })

    assert emitted[-1]["type"] == "tool_confirmation_required"
    assert bridge.conversation.items == []
    assert bridge.confirm_tool("call-confirm", True)
    assert bridge.conversation.items[-1]["type"] == "function_call_output"
    assert json.loads(bridge.conversation.items[-1]["output"])["status"] == "accepted"


def test_recovered_pending_tool_uses_data_item_when_provider_call_is_gone(tmp_path):
    harness = harness_at(tmp_path / "recovered-tool.jsonl")
    harness.record_tool_call("call-old", "human_help", {})
    harness.journal.append("tool_confirmation", {
        "callId": "call-old", "name": "human_help", "arguments": {}, "status": "pending",
    })
    bridge = make_bridge(harness)
    bridge.set_context_harness(harness)
    bridge.set_tools([{
        "name": "human_help", "description": "Ask", "requires_confirmation": True,
        "arguments": {"type": "object", "properties": {}},
    }], lambda name, arguments, confirmed: {"status": "accepted"})

    assert bridge.confirm_tool("call-old", True)
    assert bridge.conversation.items[-1]["role"] == "user"
    assert "recovered_tool_result" in bridge.conversation.items[-1]["content"][0]["text"]


def test_cancelled_response_commits_partial_assistant_transcript(tmp_path):
    harness = harness_at(tmp_path / "interrupted.jsonl")
    bridge = make_bridge(harness)
    bridge.request_response("answer", purpose="conversation")
    bridge._response_created({"response": {"id": "response-cut"}})
    bridge.route_provider_output("response.audio_transcript.delta", {
        "response_id": "response-cut", "delta": "partly spoken",
    })

    bridge._response_finished({"type": "response.cancelled", "response": {"id": "response-cut"}})

    entries = harness.journal.entries()
    assert entries[-2]["type"] == "message"
    assert entries[-2]["status"] == "interrupted"
    assert entries[-2]["message"]["content"] == "partly spoken"
    assert entries[-1]["type"] == "interruption"


def test_local_cancel_marks_provider_done_as_interrupted(tmp_path):
    harness = harness_at(tmp_path / "local-cancel-done.jsonl")
    bridge = make_bridge(harness)
    bridge.request_response("answer", purpose="conversation")
    bridge._response_created({"response": {"id": "response-local-cut"}})
    bridge.route_provider_output("response.audio_transcript.delta", {
        "response_id": "response-local-cut", "delta": "audible prefix",
    })

    assert bridge.cancel_response(reason="semantic-stop-control")
    bridge._response_finished({"type": "response.done", "response": {"id": "response-local-cut"}})

    entries = harness.journal.entries()
    assert entries[-2]["type"] == "message"
    assert entries[-2]["status"] == "interrupted"
    assert entries[-2]["message"]["content"] == "audible prefix"
    assert entries[-1]["type"] == "interruption"
    assert entries[-1]["reason"] == "local-cancel:response.done"


def test_response_done_usage_is_normalized_and_persisted(tmp_path):
    harness = harness_at(tmp_path / "usage.jsonl")
    bridge = make_bridge(harness)
    bridge.request_response("answer", purpose="conversation")
    bridge._response_created({"response": {"id": "response-usage"}})

    bridge._response_finished({
        "type": "response.done",
        "response": {
            "id": "response-usage",
            "model": "qwen-test",
            "usage": {"input_tokens": 1500, "output_tokens": 25, "total_tokens": 1525},
        },
    })

    usage = harness.journal.entries()[-1]
    assert usage["type"] == "provider_usage"
    assert usage["usage"] == {"inputTokens": 1500, "outputTokens": 25, "totalTokens": 1525}
    assert usage["model"] == "qwen-test"


def test_compaction_change_marks_provider_projection_stale(tmp_path):
    harness = harness_at(tmp_path / "projection.jsonl")
    harness.record_user("keep", source="test", turn_id="one")
    checkpoint = harness.compact(reason="provider-usage-threshold", force=True)
    built = harness.build([])
    bridge = make_bridge(harness)
    scheduled = []
    bridge._schedule_reconnect = scheduled.append

    bridge.apply_context_projection("policy", built["manifest"])

    assert bridge.connected is False
    assert scheduled == ["context-compacted"]
    assert built["manifest"]["latestCompactionId"] == checkpoint["id"]


def test_provider_reconnect_commits_active_partial_transcript(tmp_path):
    harness = harness_at(tmp_path / "reconnect-fragment.jsonl")
    bridge = make_bridge(harness)
    bridge.request_response("answer", purpose="conversation")
    bridge._response_created({"response": {"id": "response-reconnect"}})
    bridge.note_assistant_transcript("audible prefix", response_id="response-reconnect")

    bridge._persist_active_interruptions("provider-reconnect:test")

    entries = harness.journal.entries()
    assert entries[-2]["status"] == "interrupted"
    assert entries[-2]["message"]["content"] == "audible prefix"
    assert entries[-1]["reason"] == "provider-reconnect:test"
