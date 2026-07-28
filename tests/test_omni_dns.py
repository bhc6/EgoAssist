import socket
import threading

from egoassist.realtime.omni import (
    OmniBrowserBridge,
    _connect_with_resolved_ip,
    _is_active_response_conflict,
    _is_no_active_response,
    _is_fake_ip,
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


def scheduler_bridge():
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._response_lock = threading.Lock()
    bridge._response_pending = False
    bridge._active_response_ids = set()
    bridge._queued_response = None
    bridge._inflight_response = None
    bridge._response_sequence = 0
    bridge._cancel_when_created = False
    bridge._response_text = {}
    bridge._active_response_requests = {}
    bridge._assistant_transcript_buffer = ""
    bridge._last_assistant_utterance = ""
    bridge._last_assistant_utterance_at = 0.0
    bridge.last_proactive_check = 0.0
    bridge.conversation = FakeConversation()
    bridge.modalities = FakeModalities
    return bridge


def test_input_output_channels_are_distinct_and_echo_is_only_flagged(monkeypatch):
    bridge = scheduler_bridge()
    monkeypatch.setattr("egoassist.realtime.omni.time.monotonic", lambda: 10.0)
    bridge._last_assistant_utterance = "请把番茄放稳再慢慢切"
    bridge._last_assistant_utterance_at = 8.0

    echo = bridge.attribute_input_transcript("请把番茄放稳再慢慢切")
    user = bridge.attribute_input_transcript("我想改做水果沙拉")
    assert echo["speaker"] == "user"
    assert echo["sourceChannel"] == "input_audio_transcription"
    assert echo["echoRisk"] == "suspected"
    assert echo["policy"] == "observe-only-never-drop"
    assert user["echoRisk"] == "low"


def test_fake_ip_detection_is_limited_to_benchmark_network():
    assert _is_fake_ip("198.18.2.176")
    assert not _is_fake_ip("39.96.198.249")
    assert not _is_fake_ip("not-an-ip")


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
    monkeypatch.setattr("egoassist.realtime.omni.time.monotonic", lambda: 100.0)

    bridge.maybe_check_proactively(4)
    bridge.maybe_check_proactively(4)
    assert len(bridge.conversation.created) == 1
    assert bridge.conversation.created[0]["output_modalities"] == ["text"]
    assert bridge.cancel_response()
    assert bridge.conversation.cancelled == 0

    bridge._response_created({"response": {"id": "response-1"}})
    assert bridge.conversation.cancelled == 1
    bridge._response_finished({"response": {"id": "response-1"}})
    assert not bridge.cancel_response()


def test_proactive_gate_suppresses_meta_silence_and_only_schedules_useful_audio():
    suppressed = OmniBrowserBridge.parse_proactive_gate(
        '{"speak":true,"utterance":"好的，我会保持安静，不主动输出内容。","reason":"timely-help","urgency":"medium"}'
    )
    assert not suppressed["speak"]
    assert suppressed["suppressed"] == "meta-silence"

    observed = OmniBrowserBridge.parse_proactive_gate(
        '{"speak":false,"utterance":"","reason":"none","urgency":"low",'
        '"scene_summary":"用户正在整理桌面上的零件","salient_change":true}'
    )
    assert observed["speak"] is False
    assert observed["sceneSummary"] == "用户正在整理桌面上的零件"
    assert observed["salientChange"] is True

    bridge = scheduler_bridge()
    bridge.request_response("gate", priority=10, output_mode="text", purpose="proactive-gate")
    bridge._response_created({"response": {"id": "gate-1"}})
    bridge._response_text_done({
        "response_id": "gate-1",
        "text": '{"speak":true,"utterance":"先停一下，锅边有明火。","reason":"hazard","urgency":"high"}',
    })
    assert bridge._queued_response is not None
    assert bridge._queued_response.output_mode == "audio"
    assert bridge._queued_response.purpose == "proactive-utterance"
    assert bridge._queued_response.speech_policy == "protected"


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
    assert bridge._queued_response.instructions == "repeat the actual guidance"
    assert bridge.conversation.cancelled == 1

    bridge._response_finished({"response": {"id": "response-active"}})
    assert bridge.conversation.created[-1]["instructions"] == "repeat the actual guidance"
    assert bridge._response_pending


def test_conflict_requeues_inflight_until_provider_is_idle():
    bridge = scheduler_bridge()
    bridge.request_response("help now", priority=100)
    assert len(bridge.conversation.created) == 1

    bridge._response_conflict()
    assert bridge._queued_response.instructions == "help now"
    assert bridge._active_response_ids == {"provider-active"}

    bridge._response_finished({"response": {"id": "provider-active"}})
    assert len(bridge.conversation.created) == 2
    assert bridge._response_pending


def test_user_barge_in_drops_stale_proactive_request():
    bridge = scheduler_bridge()
    bridge._active_response_ids = {"response-active"}
    bridge.request_response("stale visual check", priority=10)
    assert bridge.cancel_response(drop_queued_below=100)
    assert bridge._queued_response is None
    assert bridge.conversation.cancelled == 1


def test_interrupt_waits_for_response_created_before_cancelling():
    bridge = scheduler_bridge()
    bridge.request_response("low visual check", priority=10)
    bridge.request_response("urgent control", priority=100, interrupt=True)
    assert bridge.conversation.cancelled == 0
    assert bridge._cancel_when_created

    bridge._response_created({"response": {"id": "response-low"}})
    assert bridge.conversation.cancelled == 1


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
