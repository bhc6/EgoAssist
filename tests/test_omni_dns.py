import asyncio
import socket
import threading
from collections import deque

import pytest

from egoassist.realtime.omni import (
    OmniBrowserBridge,
    _OmniPerceptionLane,
    _connect_with_resolved_ip,
    _is_active_response_conflict,
    _is_no_active_response,
    _is_response_idle_timeout,
    _is_fake_ip,
    _is_media_order_error,
    _realtime_public_ips,
)


class FakeModalities:
    AUDIO = "audio"
    TEXT = "text"


class FakeConversation:
    def __init__(self):
        self.created = []
        self.cancelled = 0

    def create_response(self, **kwargs):
        self.created.append(kwargs)

    def cancel_response(self):
        self.cancelled += 1

    def update_session(self, **kwargs):
        self.session = kwargs


def scheduler_bridge():
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._response_lock = threading.Lock()
    bridge._response_pending = False
    bridge._active_response_ids = set()
    bridge._cancelled_response_ids = set()
    bridge._speech_started_response_ids = set()
    bridge._queued_requests = deque()
    bridge._inflight_response = None
    bridge._response_sequence = 0
    bridge._cancel_when_created = False
    bridge._barge_in_pending = False
    bridge._early_terminal_response_ids = set()
    bridge._dialogue_generation = 0
    bridge._response_text = {}
    bridge._active_response_requests = {}
    bridge._output_channels_seen = set()
    bridge._suppressed_output_channels_seen = set()
    bridge._assistant_transcript_buffers = {}
    bridge._last_assistant_utterance = ""
    bridge._last_assistant_utterance_at = 0.0
    bridge.last_proactive_check = 0.0
    bridge.connected = True
    bridge.closed = False
    bridge._latest_perception_frame = None
    bridge.conversation = FakeConversation()
    bridge.modalities = FakeModalities
    bridge._schedule = lambda payload: None
    return bridge


def test_input_output_channels_are_distinct_and_echo_is_only_flagged(monkeypatch):
    bridge = scheduler_bridge()
    monkeypatch.setattr("egoassist.realtime.omni.time.monotonic", lambda: 10.0)
    bridge._last_assistant_utterance = "请把番茄放稳再慢慢切"
    bridge._last_assistant_utterance_at = 8.0

    echo = bridge.attribute_input_transcript("请把番茄放稳再慢慢切")
    user = bridge.attribute_input_transcript("我想改做水果沙拉")
    assert echo["speaker"] == "unknown"
    assert echo["sourceChannel"] == "input_audio_transcription"
    assert echo["echoRisk"] == "suspected"
    assert echo["policy"] == "observe-only-never-drop"
    assert user["echoRisk"] == "low"


def test_fake_ip_detection_is_limited_to_benchmark_network():
    assert _is_fake_ip("198.18.2.176")
    assert not _is_fake_ip("39.96.198.249")
    assert not _is_fake_ip("not-an-ip")


def test_dual_stack_uses_dns_ipv4_without_pinning_provider_addresses(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_REALTIME_IPS", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2001:db8::1", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 443)),
    ])
    assert _realtime_public_ips() == ["203.0.113.10"]


def test_session_updates_only_on_changed_policy_tools_or_connection():
    bridge = scheduler_bridge()
    bridge.voice, bridge.instructions = "Ethan", "policy"
    calls = []
    bridge.conversation.update_session = lambda **kwargs: calls.append(kwargs)
    bridge._configure_dialogue(bridge.conversation)
    bridge._configure_dialogue(bridge.conversation)
    assert len(calls) == 1
    bridge._provider_tools = [{"name": "reference"}]
    bridge._configure_dialogue(bridge.conversation)
    assert len(calls) == 2
    replacement = FakeConversation()
    replacement.update_session = lambda **kwargs: calls.append(kwargs)
    bridge._configure_dialogue(replacement)
    assert len(calls) == 3


def test_provider_active_response_error_is_internal_backpressure():
    payload = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Conversation already has an active response",
        },
    }
    assert _is_active_response_conflict(payload)


def test_provider_no_active_response_is_benign_cancel_race():
    payload = {"type": "error", "error": {"message": "Conversation has none active response"}}
    assert _is_no_active_response(payload)
    bridge = scheduler_bridge()
    bridge._active_response_ids = {"stale-response"}
    bridge._active_response_requests = {}
    bridge._barge_in_pending = True
    bridge._response_absent_after_cancel()
    assert bridge._active_response_ids == set()
    assert not bridge._barge_in_pending


def test_provider_response_idle_timeout_is_recoverable_session_error():
    payload = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "response idle timeout",
            "message": "Your session was closed because no response was generated for 300 seconds.",
        },
    }
    assert _is_response_idle_timeout(payload)


def test_provider_media_ordering_error_is_recoverable_protocol_error():
    assert _is_media_order_error({
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "Error append image before append audio."},
    })


def test_idle_timeout_recovery_replaces_dialogue_session_and_emits_ready():
    async def scenario():
        bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
        emitted = []
        traced = []

        class OldConversation:
            closed = False

            def close(self):
                self.closed = True

        old = OldConversation()
        bridge.closed = False
        bridge.connected = True
        bridge.conversation = old
        bridge._dialogue_generation = 1
        bridge._reconnect_scheduled = True
        bridge.emit = lambda payload: _append_async(emitted, payload)
        bridge._trace = lambda event, payload, response_id=None: traced.append((event, payload))
        bridge.provider_lanes_snapshot = lambda: {"dialogue": {"connected": bridge.connected}}

        async def connect_dialogue():
            bridge.connected = True

        bridge._connect_dialogue = connect_dialogue
        await bridge._recover_dialogue("response-idle-timeout")
        assert old.closed
        assert bridge.connected
        assert [item["type"] for item in emitted] == ["reconnecting", "ready"]
        assert emitted[-1]["reconnected"] is True
        assert any(event == "provider.session.reconnecting" for event, _ in traced)
        assert bridge._reconnect_scheduled is False

    async def _append_async(target, value):
        target.append(value)

    asyncio.run(scenario())


def test_dialogue_vad_detects_turns_but_application_owns_response_creation():
    bridge = scheduler_bridge()
    bridge.voice = "Ethan"
    bridge.instructions = "generic"
    bridge._configure_dialogue(bridge.conversation)
    assert bridge.conversation.session["enable_turn_detection"] is True
    assert bridge.conversation.session["turn_detection_type"] == "server_vad"
    assert bridge.conversation.session["turn_detection_param"] == {
        "create_response": False,
        "interrupt_response": True,
    }


def test_fake_dns_uses_doh_records(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_REALTIME_IPS", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.176", 443)),
    ])

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"Answer": [
                {"type": 5, "data": "alias.example"},
                {"type": 1, "data": "39.96.198.249"},
            ]}

    monkeypatch.setattr("egoassist.realtime.omni.requests.get", lambda *args, **kwargs: Response())
    assert _realtime_public_ips() == ["39.96.198.249"]


def test_fake_dns_cascades_when_first_doh_endpoint_is_blocked(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_REALTIME_IPS", raising=False)
    monkeypatch.setenv("DASHSCOPE_DOH_URLS", "https://blocked.example/resolve,https://working.example/resolve")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.176", 443)),
    ])
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"Answer": [{"type": 1, "data": "39.96.198.249"}]}

    def resolve(url, **kwargs):
        calls.append(url)
        if "blocked" in url:
            raise OSError("socket blocked")
        return Response()

    monkeypatch.setattr("egoassist.realtime.omni.requests.get", resolve)
    assert _realtime_public_ips() == ["39.96.198.249"]
    assert calls == ["https://blocked.example/resolve", "https://working.example/resolve"]


def test_fake_dns_reports_actionable_error_when_all_resolvers_are_blocked(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_REALTIME_IPS", raising=False)
    monkeypatch.setenv("DASHSCOPE_DOH_URLS", "https://blocked.example/resolve")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.176", 443)),
    ])
    monkeypatch.setattr("egoassist.realtime.omni.requests.get", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("socket blocked")))
    with pytest.raises(ConnectionError, match="DASHSCOPE_REALTIME_IPS"):
        _realtime_public_ips()


def test_connection_override_preserves_global_resolver(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_REALTIME_IPS", "39.96.198.249")
    original = socket.getaddrinfo

    class Conversation:
        ws = None

        def connect(self):
            self.addresses = socket.getaddrinfo("dashscope.aliyuncs.com", 443, type=socket.SOCK_STREAM)

    conversation = Conversation()
    _connect_with_resolved_ip(conversation)
    assert conversation.addresses[0][4][0] == "39.96.198.249"
    assert socket.getaddrinfo is original


def test_proactive_checks_coalesce_while_response_is_active(monkeypatch):
    bridge = scheduler_bridge()
    requested = []
    bridge.perception = type("Perception", (), {"request": lambda self, frame: requested.append(frame) or True})()
    bridge._latest_perception_frame = "latest-frame"
    monkeypatch.setattr("egoassist.realtime.omni.time.monotonic", lambda: 100.0)

    bridge.maybe_check_proactively(4)
    bridge.maybe_check_proactively(4)
    assert requested == ["latest-frame"]
    assert bridge.conversation.created == []


def test_proactive_gate_uses_structured_policy_not_utterance_keywords():
    observed = OmniBrowserBridge.parse_proactive_gate(
        '{"speak":false,"utterance":"","reason":"none","urgency":"low",'
        '"scene_summary":"用户正在整理桌面上的零件","salient_change":true}'
    )
    assert observed["speak"] is False
    assert observed["sceneSummary"] == "用户正在整理桌面上的零件"
    assert observed["salientChange"] is True

    bridge = scheduler_bridge()
    bridge._trace = lambda *args, **kwargs: None
    bridge._handle_proactive_decision(OmniBrowserBridge.parse_proactive_gate(
        '{"speak":true,"utterance":"先停一下，锅边有明火。","reason":"hazard","urgency":"high"}'
    ))
    assert bridge._inflight_response is not None
    assert bridge._inflight_response.output_mode == "audio"
    assert bridge._inflight_response.purpose == "proactive-utterance"
    assert bridge._inflight_response.speech_policy == "protected"


def test_dialogue_lane_rejects_internal_gate_requests():
    bridge = scheduler_bridge()
    with pytest.raises(ValueError, match="isolated perception lane"):
        bridge.request_response("gate", priority=10, output_mode="text", purpose="proactive-gate")
    assert bridge.conversation.created == []


def test_perception_lane_uses_silence_before_frame_and_never_microphone_audio():
    loop = asyncio.new_event_loop()
    events = []

    class Conversation:
        def append_audio(self, value):
            events.append(("audio", value))

        def update_session(self, **kwargs):
            pass

        def append_video(self, value):
            events.append(("video", value))

        def commit(self):
            events.append(("commit", None))

        def create_response(self, **kwargs):
            events.append(("response", kwargs))

    lane = _OmniPerceptionLane(
        api_key="test", model="test", voice="Ethan", loop=loop,
        conversation_cls=object, callback_cls=object, modalities=FakeModalities,
        on_decision=lambda decision: None, trace=lambda *args, **kwargs: None,
    )
    lane.connected = True
    lane.conversation = Conversation()
    assert lane.request("camera-frame")
    assert [item[0] for item in events] == ["audio", "video", "commit", "response"]
    assert events[0][1] == lane._SILENT_PCM16
    assert events[1][1] == "camera-frame"
    assert lane.snapshot()["microphoneShared"] is False
    loop.close()


def test_silent_or_malformed_gate_never_schedules_user_visible_response():
    for gate_text in (
        '{"speak":false,"utterance":"","reason":"none","urgency":"low"}',
        'not valid json',
    ):
        bridge = scheduler_bridge()
        bridge._trace = lambda *args, **kwargs: None
        bridge._handle_proactive_decision(OmniBrowserBridge.parse_proactive_gate(gate_text))
        assert not bridge._queued_requests
        assert bridge._inflight_response is None
        assert bridge._last_assistant_utterance == ""


def test_perception_text_done_does_not_release_next_frame_before_terminal_event():
    async def scenario():
        decisions, traces, requests = [], [], []

        class Conversation:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def append_audio(self, value):
                pass

            def append_video(self, value):
                requests.append(value)

            def commit(self):
                pass

            def create_response(self, **kwargs):
                pass

            def update_session(self, **kwargs):
                pass

        lane = _OmniPerceptionLane(api_key="test", model="test", voice="Ethan", loop=asyncio.get_running_loop(),
            conversation_cls=Conversation, callback_cls=object, modalities=FakeModalities,
            on_decision=lambda decision: decisions.append(decision), trace=lambda event, payload, **kw: traces.append((event, payload)))
        assert lane.snapshot()["connectionState"] == "idle"
        lane.conversation = lane._new_conversation(0)
        lane.connected = True
        callback = lane.conversation.callback
        lane.request("first-frame")
        callback.on_event({"type": "response.created", "response": {"id": "one"}})
        lane.request("second-frame")
        callback.on_event({"type": "response.text.done", "text": '{"speak":false,"utterance":"","reason":"none","urgency":"low"}'})
        await asyncio.sleep(0)
        assert requests == ["first-frame"] and lane.active and not decisions
        callback.on_event({"type": "response.done", "response": {"id": "one", "status": "completed", "usage": {"input_tokens": 8, "output_tokens": 3}}})
        await asyncio.sleep(0)
        assert requests == ["first-frame", "second-frame"] and lane.active and len(decisions) == 1
        finished = next(p for e, p in traces if e == "perception.response.finished")
        assert finished["usage"]["inputTokens"] == 8 and finished["latencyMs"] >= 0
    asyncio.run(scenario())


def test_exact_response_id_controls_conversation_visibility():
    bridge = scheduler_bridge()
    bridge.perception = type("Perception", (), {"active": True})()
    emitted = []
    bridge._schedule = emitted.append
    bridge._trace = lambda *args, **kwargs: None
    bridge.request_response("answer", priority=50, purpose="conversation")
    bridge._response_created({"response": {"id": "speech-1"}})
    assert bridge.route_provider_output(
        "response.audio_transcript.done", {"response_id": "speech-1", "transcript": "我记住了，请继续。"},
    )
    assert [item["text"] for item in emitted if item["type"] == "assistant_transcript"] == ["我记住了，请继续。"]
    assert emitted[-1]["purpose"] == "conversation"
    assert emitted[-1]["responseId"] == "speech-1"
    assert bridge._last_assistant_utterance == "我记住了，请继续。"
    assert bridge.perception.active is True


def test_unattributed_provider_output_fails_closed():
    bridge = scheduler_bridge()
    emitted = []
    traced = []
    bridge._schedule = emitted.append
    bridge._trace = lambda event, payload, response_id=None: traced.append((event, payload, response_id))

    assert not bridge.route_provider_output("response.audio.delta", {"delta": "unknown"})
    assert not bridge.route_provider_output(
        "response.audio_transcript.done", {"response_id": "missing", "transcript": "internal"},
    )
    assert emitted == []
    assert all(item[0] == "response.output.suppressed" for item in traced)


def test_provider_created_user_turn_is_explicitly_classified_as_conversation():
    bridge = scheduler_bridge()
    emitted = []
    bridge._schedule = emitted.append
    bridge._trace = lambda *args, **kwargs: None
    bridge._response_created({"response": {"id": "auto-1"}})

    request = bridge._active_response_requests["auto-1"]
    assert request.purpose == "conversation"
    assert request.reason == "provider-auto-user-turn"
    assert bridge.route_provider_output(
        "response.audio_transcript.done", {"response_id": "auto-1", "transcript": "这是正常回答。"},
    )
    assert emitted[-1]["type"] == "assistant_transcript"


def test_old_done_does_not_clear_new_pending_response():
    bridge = scheduler_bridge()
    bridge._response_pending = True
    bridge._active_response_ids = {"response-old"}
    bridge._response_finished({"response": {"id": "response-old"}})
    assert bridge._response_pending


def test_high_priority_control_replaces_queued_proactive_check():
    bridge = scheduler_bridge()
    bridge._active_response_ids = {"response-active"}
    bridge.request_response("low visual check", priority=10)
    bridge.request_response("repeat the actual guidance", priority=100, interrupt=True)
    assert bridge._queued_requests[0].instructions == "repeat the actual guidance"
    assert bridge.conversation.cancelled == 1

    bridge._response_finished({"response": {"id": "response-active"}})
    assert bridge.conversation.created[-1]["instructions"] == "repeat the actual guidance"
    assert bridge._response_pending


def test_conflict_requeues_inflight_until_provider_is_idle():
    bridge = scheduler_bridge()
    bridge.request_response("help now", priority=100)
    assert len(bridge.conversation.created) == 1

    bridge._response_conflict()
    assert bridge._queued_requests[0].instructions == "help now"
    assert bridge._active_response_ids == {"provider-active"}

    bridge._response_finished({"response": {"id": "provider-active"}})
    assert len(bridge.conversation.created) == 2
    assert bridge._response_pending


def test_conflict_sentinel_accepts_terminal_event_real_response_id():
    bridge = scheduler_bridge()
    bridge.request_response("help now", priority=100)
    bridge._response_conflict()

    bridge._response_finished({"type": "response.done", "response": {"id": "actual-provider-id"}})

    assert bridge._active_response_ids == set()
    assert bridge._response_pending
    assert len(bridge.conversation.created) == 2


def test_conflict_sentinel_has_watchdog_recovery():
    bridge = scheduler_bridge()
    scheduled = []
    bridge._schedule_response_watchdog = lambda *args: scheduled.append(args)
    bridge.request_response("help now", priority=100)

    bridge._response_conflict()

    assert scheduled[-1] == ("finish", "provider-active", 0)


def test_user_barge_in_drops_stale_proactive_request():
    bridge = scheduler_bridge()
    bridge._active_response_ids = {"response-active"}
    bridge.request_response("stale visual check", priority=10)
    assert bridge.cancel_response(drop_queued_below=100)
    assert not bridge._queued_requests
    assert bridge.conversation.cancelled == 1


def test_natural_speech_started_cancels_immediate_guidance():
    bridge = scheduler_bridge()
    bridge._trace = lambda *args, **kwargs: None
    bridge.request_response("ordinary guidance", speech_policy="immediate")
    bridge._response_created({"response": {"id": "speech-immediate"}})

    assert bridge.handle_user_speech_started() == "cancelled"
    assert bridge.conversation.cancelled == 1
    assert "speech-immediate" in bridge._cancelled_response_ids
    assert bridge.handle_user_speech_started(trigger="transcript-fallback") == "already-handled"
    assert bridge.conversation.cancelled == 1


def test_natural_speech_started_finishes_clause_then_cancels():
    bridge = scheduler_bridge()
    emitted = []
    bridge._schedule = emitted.append
    bridge._trace = lambda *args, **kwargs: None
    bridge.request_response("short clarification", speech_policy="finish-clause")
    bridge._response_created({"response": {"id": "speech-clause"}})

    assert bridge.handle_user_speech_started() == "deferred"
    assert bridge.conversation.cancelled == 0
    bridge.note_assistant_transcript("先确认这个位置。", response_id="speech-clause")
    assert bridge.conversation.cancelled == 1
    assert emitted[-1] == {"type": "interrupted"}


def test_natural_speech_started_bounds_protected_utterance():
    bridge = scheduler_bridge()
    scheduled = []
    bridge._trace = lambda *args, **kwargs: None
    bridge._schedule_protection_expiry = scheduled.append
    bridge._schedule = lambda *args, **kwargs: None
    bridge.request_response(
        "brief safety warning", speech_policy="protected", max_protection_ms=2500,
    )
    bridge._response_created({"response": {"id": "speech-protected"}})

    assert bridge.handle_user_speech_started() == "protected"
    assert scheduled == [2500]
    assert bridge.conversation.cancelled == 0
    bridge._expire_protection(2500)
    assert bridge.conversation.cancelled == 1


def test_expired_old_protection_timer_cannot_cancel_new_response():
    bridge = scheduler_bridge()
    bridge.request_response("old safety phrase", speech_policy="protected")
    bridge._response_created({"response": {"id": "old-protected"}})
    bridge._response_finished({"type": "response.done", "response": {"id": "old-protected"}})
    bridge.request_response("new safety phrase", speech_policy="protected")
    bridge._response_created({"response": {"id": "new-protected"}})

    bridge._expire_protection(2500, frozenset({"old-protected"}), 0)
    assert bridge.conversation.cancelled == 0
    assert not bridge.cancel_response(expected_response_ids=frozenset({"old-protected"}))
    bridge._expire_protection(2500, frozenset({"new-protected"}), 0)
    assert bridge.conversation.cancelled == 1

    async def real_timer():
        bridge = scheduler_bridge()
        bridge.loop = asyncio.get_running_loop()
        cancelled = asyncio.Event()
        def on_cancel():
            bridge.conversation.cancelled += 1
            cancelled.set()
        bridge.conversation.cancel_response = on_cancel
        bridge.request_response("protected", speech_policy="protected", max_protection_ms=250)
        bridge._response_created({"response": {"id": "timed-protected"}})
        assert bridge.handle_user_speech_started() == "protected"
        await asyncio.wait_for(cancelled.wait(), 3)
        assert bridge.conversation.cancelled == 1

    asyncio.run(real_timer())

    pending = scheduler_bridge()
    pending.request_response("not yet created", speech_policy="protected")
    assert pending.handle_user_speech_started() == "cancelled"
    pending._response_created({"response": {"id": "cancel-on-arrival"}})
    assert pending.conversation.cancelled == 1


def test_cancelled_response_drops_late_audio_and_transcript_output():
    bridge = scheduler_bridge()
    emitted = []
    traced = []
    bridge._schedule = emitted.append
    bridge._trace = lambda event, payload, response_id=None: traced.append((event, payload, response_id))
    bridge.request_response("answer", priority=50, purpose="conversation")
    bridge._response_created({"response": {"id": "speech-cancelled"}})

    assert bridge.cancel_response()
    assert not bridge.route_provider_output(
        "response.audio.delta", {"response_id": "speech-cancelled", "delta": "late-audio"},
    )
    assert not bridge.route_provider_output(
        "response.audio_transcript.delta",
        {"response_id": "speech-cancelled", "delta": "late transcript"},
    )
    assert not [item for item in emitted if item["type"] in {"audio", "assistant_transcript"}]
    assert {item[1]["reason"] for item in traced if item[0] == "response.output.suppressed"} == {
        "cancel-requested"
    }

    bridge._response_finished({"type": "response.cancelled", "response": {"id": "speech-cancelled"}})
    assert "speech-cancelled" not in bridge._cancelled_response_ids


def test_interrupt_waits_for_response_created_before_cancelling():
    bridge = scheduler_bridge()
    bridge.request_response("low visual check", priority=10)
    bridge.request_response("urgent control", priority=100, interrupt=True)
    assert bridge.conversation.cancelled == 0
    assert bridge._cancel_when_created

    bridge._response_created({"response": {"id": "response-low"}})
    assert bridge.conversation.cancelled == 1


def test_pending_finish_clause_policy_is_preserved_before_created():
    bridge = scheduler_bridge()
    bridge._trace = lambda *args, **kwargs: None
    bridge.request_response("finish this clause", speech_policy="finish-clause")

    assert bridge.handle_user_speech_started() == "deferred"
    assert bridge.conversation.cancelled == 0
    assert bridge._barge_in_pending


def test_create_watchdog_recovers_queue_without_replaying_orphan():
    bridge = scheduler_bridge()
    traced = []
    reconnects = []
    bridge._trace = lambda event, payload, response_id=None: traced.append((event, payload))
    bridge._schedule_reconnect = reconnects.append
    bridge.request_response("possibly accepted", priority=10)
    bridge.request_response("new user turn", priority=100)

    bridge._response_watchdog_fired("create", 1, 0)

    assert reconnects == ["response-create-timeout"]
    assert bridge._queued_requests[0].instructions == "new user turn"
    assert any(event == "response.watchdog.timeout" for event, _ in traced)
    bridge._reset_scheduler_after_reconnect()
    assert [item["instructions"] for item in bridge.conversation.created] == [
        "possibly accepted", "new user turn",
    ]


def test_finish_watchdog_recovers_active_orphan_and_preserves_queue():
    bridge = scheduler_bridge()
    reconnects = []
    bridge._trace = lambda *args, **kwargs: None
    bridge._schedule_reconnect = reconnects.append
    bridge.request_response("old answer")
    bridge._response_created({"response": {"id": "orphan"}})
    bridge.request_response("new answer", priority=100)

    bridge._response_watchdog_fired("finish", "orphan", 0)

    assert reconnects == ["response-finish-timeout"]
    bridge._reset_scheduler_after_reconnect()
    assert bridge._response_pending
    assert bridge._inflight_response.instructions == "new answer"


def test_provider_successor_reconciles_active_orphan():
    bridge = scheduler_bridge()
    traced = []
    bridge._trace = lambda event, payload, response_id=None: traced.append((event, payload))
    bridge.request_response("old answer")
    bridge._response_created({"response": {"id": "old"}})

    bridge._response_created({"response": {"id": "successor"}})

    assert bridge._active_response_ids == {"successor"}
    assert "old" not in bridge._active_response_requests
    assert any(event == "response.orphan.reconciled" for event, _ in traced)


def test_terminal_before_created_reconciles_and_dispatches_queue():
    bridge = scheduler_bridge()
    bridge._trace = lambda *args, **kwargs: None
    bridge.request_response("first")
    bridge.request_response("second", priority=100)

    bridge._response_finished({"type": "response.done", "response": {"id": "first-id"}})
    bridge._response_created({"response": {"id": "first-id"}})

    assert bridge._response_pending
    assert bridge._inflight_response.instructions == "second"
    assert bridge._active_response_ids == set()


def test_duplicate_created_after_finish_does_not_resurrect_response():
    bridge = scheduler_bridge()
    bridge._trace = lambda *args, **kwargs: None
    bridge.request_response("answer")
    bridge._response_created({"response": {"id": "completed"}})
    bridge._response_finished({"type": "response.done", "response": {"id": "completed"}})

    bridge._response_created({"response": {"id": "completed"}})

    assert bridge._active_response_ids == set()
    assert not bridge._response_pending


def test_long_ordered_scheduler_replay_never_leaves_orphan_state():
    bridge = scheduler_bridge()
    bridge._trace = lambda *args, **kwargs: None
    for index in range(500):
        bridge.request_response(f"turn-{index}")
        response_id = f"response-{index}"
        bridge._response_created({"response": {"id": response_id}})
        bridge._response_created({"response": {"id": response_id}})
        bridge._response_finished({"type": "response.done", "response": {"id": response_id}})
        bridge._response_finished({"type": "response.done", "response": {"id": response_id}})

    assert not bridge._response_pending
    assert bridge._active_response_ids == set()
    assert not bridge._queued_requests


def test_video_waits_for_audio_and_coalesces_pending_frames():
    class Conversation:
        def __init__(self):
            self.events = []

        def append_audio(self, value):
            self.events.append(("audio", value))

        def append_video(self, value):
            self.events.append(("video", value))

    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge.conversation = Conversation()
    bridge._media_lock = threading.Lock()
    bridge._pending_video = None

    assert not bridge.append_video("old-frame")
    assert not bridge.append_video("latest-frame")
    assert bridge.conversation.events == []
    assert bridge.append_audio("pcm-1")
    assert bridge.conversation.events == [("audio", "pcm-1"), ("video", "latest-frame")]

    assert not bridge.append_video("waiting-frame")
    assert bridge.append_audio("pcm-2")
    assert bridge.conversation.events[-2:] == [("audio", "pcm-2"), ("video", "waiting-frame")]


def test_video_never_uses_a_stale_audio_append_as_ordering_credit():
    class Conversation:
        def __init__(self):
            self.events = []

        def append_audio(self, value):
            self.events.append(("audio", value))

        def append_video(self, value):
            self.events.append(("video", value))

    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge.conversation = Conversation()
    bridge._media_lock = threading.Lock()
    bridge._pending_video = None

    assert not bridge.append_audio("audio-that-may-be-auto-committed")
    assert not bridge.append_video("late-frame")
    assert bridge.conversation.events == [("audio", "audio-that-may-be-auto-committed")]

    assert bridge.append_audio("fresh-audio")
    assert bridge.conversation.events[-2:] == [("audio", "fresh-audio"), ("video", "late-frame")]


def test_reconnect_audio_overflow_is_bounded_and_explicitly_reported():
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._buffered_audio = __import__("collections").deque()
    bridge._buffered_audio_limit = 2
    bridge._dropped_audio_chunks = 0
    traced = []
    emitted = []
    bridge._trace = lambda event, payload, response_id=None: traced.append((event, payload))
    bridge._schedule = emitted.append

    bridge._buffer_audio_for_reconnect("oldest")
    bridge._buffer_audio_for_reconnect("middle")
    bridge._buffer_audio_for_reconnect("newest")

    assert list(bridge._buffered_audio) == ["middle", "newest"]
    assert traced[-1][0] == "provider.media.gap"
    assert traced[-1][1]["droppedChunks"] == 1
    assert emitted[-1]["type"] == "media_gap"


def test_production_video_flush_waits_for_provider_speech_segment():
    class Conversation:
        def __init__(self):
            self.events = []

        def append_audio(self, value):
            self.events.append(("audio", value))

        def append_video(self, value):
            self.events.append(("video", value))

    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge.conversation = Conversation()
    bridge._media_lock = threading.Lock()
    bridge._pending_video = None
    bridge._input_speech_active = False
    bridge._dialogue_generation = 2
    bridge._input_segment_sequence = 0
    bridge._pending_input_segments = __import__("collections").deque()
    bridge._trace = lambda *args, **kwargs: None

    bridge.append_video("frame")
    assert not bridge.append_audio("pre-speech-audio")
    assert bridge.conversation.events == [("audio", "pre-speech-audio")]
    bridge._input_segment_started({"audio_start_ms": 10})
    assert bridge.conversation.events[-1] == ("video", "frame")
    assert not bridge.append_audio("speech-audio")
    assert bridge.conversation.events[-1] == ("audio", "speech-audio")


def test_input_segments_have_auditable_terminal_state():
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._media_lock = threading.Lock()
    bridge._dialogue_generation = 3
    bridge._input_segment_sequence = 0
    bridge._pending_input_segments = __import__("collections").deque()
    traced = []
    bridge._trace = lambda event, payload, response_id=None: traced.append((event, payload))

    segment_id = bridge._input_segment_started({"audio_start_ms": 100})
    assert bridge.has_unresolved_user_input()
    bridge._input_segment_stopped({"audio_end_ms": 900})
    assert bridge.has_unresolved_user_input()
    bridge._input_segment_transcribed("记住这句话")
    assert not bridge.has_unresolved_user_input()

    assert [event for event, _ in traced] == [
        "input.segment.started", "input.segment.stopped", "input.segment.transcribed",
    ]
    assert {payload["segmentId"] for _, payload in traced} == {segment_id}
