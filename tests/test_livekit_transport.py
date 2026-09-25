import asyncio
import base64
import json
from aiohttp.test_utils import TestClient, TestServer
from livekit import api

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.realtime import (
    LIVEKIT_DATA_TOPIC,
    LiveKitInteractionTransport,
    LiveKitSettings,
    livekit_capability,
)
from run_egoassist import create_app


class FakeParticipant:
    def __init__(self):
        self.published = []

    async def publish_data(self, payload, **kwargs):
        self.published.append((payload, kwargs))


class FakeRoom:
    def __init__(self):
        self.handlers = {}
        self.connected = None
        self.disconnected = False
        self.local_participant = FakeParticipant()

    def on(self, name, callback):
        self.handlers[name] = callback

    async def connect(self, url, token):
        self.connected = (url, token)

    async def disconnect(self):
        self.disconnected = True


class FakeAudioSource:
    def __init__(self):
        self.frames = []
        self.cleared = False

    async def capture_frame(self, frame):
        self.frames.append(frame)

    def clear_queue(self):
        self.cleared = True


def settings():
    return LiveKitSettings(
        url="ws://livekit.test:7880",
        api_key="devkey",
        api_secret="test-secret-that-is-at-least-32-bytes-long",
        token_ttl_seconds=300,
    )


def test_livekit_browser_token_is_room_scoped_and_does_not_expose_secret():
    transport = LiveKitInteractionTransport(settings(), room_factory=FakeRoom)
    token = transport.issue_browser_token("case-7", "participant-9")
    claims = api.TokenVerifier(settings().api_key, settings().api_secret).verify(token)

    assert claims.identity == "participant-9"
    assert claims.video.room == "egoassist-case-7"
    assert claims.video.room_join is True
    assert claims.video.can_publish is True
    assert claims.video.can_subscribe is True
    assert claims.video.can_publish_data is True
    assert set(claims.video.can_publish_sources) == {"microphone", "camera"}
    assert "secret" not in json.dumps(settings().public_summary())


def test_livekit_capability_is_truthful_when_configuration_is_absent(monkeypatch):
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)

    assert LiveKitSettings.from_env() is None
    assert livekit_capability(None)["status"] == "unavailable"
    assert livekit_capability(settings())["status"] == "configured"


def test_livekit_normalizes_control_events_and_publishes_data():
    async def run():
        room = FakeRoom()
        transport = LiveKitInteractionTransport(settings(), room_factory=lambda: room)
        await transport.connect("case-1")
        events = transport.events("case-1")

        connected = await anext(events)
        assert connected == {
            "type": "transport_status",
            "status": "connected",
            "transport": "livekit",
        }

        packet = type("Packet", (), {
            "topic": LIVEKIT_DATA_TOPIC,
            "data": json.dumps({"type": "control", "transcript": "stop"}).encode(),
            "participant": type("Participant", (), {"identity": "participant-1"})(),
        })()
        room.handlers["data_received"](packet)
        event = await anext(events)
        assert event["type"] == "control"
        assert event["participantId"] == "participant-1"
        assert event["transport"] == "livekit"

        await transport.publish("case-1", {"type": "state", "value": 2})
        raw, options = room.local_participant.published[-1]
        assert json.loads(raw) == {"type": "state", "value": 2}
        assert options == {"reliable": True, "topic": LIVEKIT_DATA_TOPIC}

        await transport.close("case-1")
        assert room.disconnected is True
    asyncio.run(run())


def test_livekit_audio_output_and_barge_in_use_one_published_source():
    async def run():
        room = FakeRoom()
        transport = LiveKitInteractionTransport(settings(), room_factory=lambda: room)
        await transport.connect("case-2")
        source = FakeAudioSource()
        transport._sessions["case-2"].output_source = source
        pcm = b"\x01\x00\xfe\xff"

        await transport.publish("case-2", {
            "type": "audio",
            "audio": base64.b64encode(pcm).decode("ascii"),
        })
        await transport.publish("case-2", {"type": "interrupted"})

        assert len(source.frames) == 1
        assert source.frames[0].data.cast("B").tobytes() == pcm
        assert source.frames[0].sample_rate == 24_000
        assert source.cleared is True
        source.cleared = False
        await transport.publish("case-2", {"type": "playback_stop"})
        assert source.cleared is True
        await transport.close("case-2")
    asyncio.run(run())


def test_livekit_queue_keeps_explicit_gap_and_latest_event_under_pressure():
    async def run():
        room = FakeRoom()
        transport = LiveKitInteractionTransport(settings(), room_factory=lambda: room)
        await transport.connect("case-3")
        session = transport._sessions["case-3"]
        while not session.queue.full():
            session.queue.put_nowait({"type": "video", "image": "old"})

        transport._put_event(session, {"type": "video", "image": "latest"})
        drained = []
        while not session.queue.empty():
            drained.append(session.queue.get_nowait())

        assert drained[-2]["type"] == "media_gap"
        assert drained[-1] == {"type": "video", "image": "latest"}
        await transport.close("case-3")
    asyncio.run(run())


def test_observation_stop_clears_only_observation_audio_and_blocks_delayed_publish():
    async def run():
        transport = LiveKitInteractionTransport(settings(), room_factory=FakeRoom)
        await transport.connect("ambient-case")
        session = transport._sessions["ambient-case"]
        source = FakeAudioSource()
        session.output_source = source
        pcm = base64.b64encode(b"\x01\x00\xfe\xff").decode("ascii")
        observation = {"type": "audio", "audio": pcm, "origin": {"observationReviewId": "review"}}
        user = {"type": "audio", "audio": pcm, "origin": {"turnId": "question"}}
        assert await transport.publish("ambient-case", observation) is True
        transport.set_observation_speech_allowed("ambient-case", False)
        assert source.cleared
        assert await transport.publish("ambient-case", observation) is False
        assert len(source.frames) == 1

        source.cleared = False
        await transport.publish("ambient-case", user)
        transport.set_observation_speech_allowed("ambient-case", False)
        assert len(source.frames) == 2 and not source.cleared
        transport.set_observation_speech_allowed("ambient-case", True)
        await transport.publish("ambient-case", observation)
        await transport.publish("ambient-case", user)
        assert source.cleared  # A user answer takes over any remaining observation queue.
        source.cleared = False
        transport.set_observation_speech_allowed("ambient-case", False)
        assert not source.cleared

        # Revoke while creation of the output track is awaiting the transport.
        entered = asyncio.Event()
        release = asyncio.Event()
        async def delayed_output(_session):
            entered.set()
            await release.wait()
            return source
        transport._ensure_output_source = delayed_output
        transport.set_observation_speech_allowed("ambient-case", True)
        pending = asyncio.create_task(transport.publish("ambient-case", observation))
        await entered.wait()
        transport.set_observation_speech_allowed("ambient-case", False)
        release.set()
        assert await pending is False
        assert len(source.frames) == 4
        await transport.close("ambient-case")
    asyncio.run(run())


def test_reminder_and_question_output_permissions_are_independent():
    async def run():
        transport = LiveKitInteractionTransport(settings(), room_factory=FakeRoom)
        await transport.connect('kinds')
        session = transport._sessions['kinds']
        session.output_source = source = FakeAudioSource()
        pcm = base64.b64encode(b'\x01\x00').decode()
        async def publish(kind):
            await transport.publish('kinds', {'type': 'audio', 'audio': pcm,
                'origin': {'observationReviewId': 'review', 'observationKind': kind}})
        transport.set_observation_speech_allowed('kinds', True, kinds={'reminder'})
        await publish('clarification')
        await publish('reminder')
        assert len(source.frames) == 1
        transport.set_observation_speech_allowed('kinds', True, kinds={'reminder'})
        assert not source.cleared
        transport.set_observation_speech_allowed('kinds', True, kinds={'clarification'})
        assert source.cleared
        await publish('reminder')
        await publish('clarification')
        assert len(source.frames) == 2
        await transport.close('kinds')
    asyncio.run(run())


def test_livekit_token_api_is_authenticated_and_case_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", settings().url)
    monkeypatch.setenv("LIVEKIT_API_KEY", settings().api_key)
    monkeypatch.setenv("LIVEKIT_API_SECRET", settings().api_secret)

    async def run():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            unauthenticated = await client.post("/api/livekit/token", json={})
            assert unauthenticated.status == 401
            registered = await client.post("/api/auth/register", json={
                "username": "livekit-user", "password": "livekit-pass-123",
            })
            assert registered.status == 200
            session_id = (await (await client.post("/api/sessions")).json())["session"]["id"]
            response = await client.post("/api/livekit/token", json={"sessionId": session_id})
            assert response.status == 200
            payload = await response.json()
            assert payload["enabled"] is True
            assert payload["room"] == f"egoassist-{session_id}"
            assert payload["url"] == settings().url
            assert "api_secret" not in json.dumps(payload).casefold()
            claims = api.TokenVerifier(settings().api_key, settings().api_secret).verify(payload["token"])
            assert claims.video.room == payload["room"]
            assert claims.identity == payload["participantId"]

    asyncio.run(run())


def test_livekit_token_api_returns_explicit_fallback_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)

    async def run():
        app = create_app(
            str(BUILTIN_SKILLS_ROOT), None,
            str(tmp_path / "memory.json"), "test-realtime", "Ethan",
        )
        async with TestClient(TestServer(app)) as client:
            await client.post("/api/auth/register", json={
                "username": "fallback-user", "password": "fallback-pass-123",
            })
            response = await client.post("/api/livekit/token", json={})
            payload = await response.json()
            assert payload == {
                "enabled": False,
                "transport": "websocket-fallback",
                "capability": livekit_capability(None),
            }

    asyncio.run(run())
