import asyncio
import base64
from functools import partial
import json

import pytest

from egoassist.realtime.modular import ModularConversation, ModularProvider, SpeechSegments, settings
from egoassist.realtime.omni import OmniBrowserBridge


class RecordingCallback:
    def __init__(self): self.events = []
    def on_open(self): pass
    def on_event(self, event): self.events.append(event)


class FakeProvider:
    def __init__(self, api_key, config, audit):
        self.audit = audit
        self.messages = []
        self.frames = []
        self.pcm = []
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.fail = False

    async def text_stream(self, messages):
        self.messages.append(messages)
        self.started.set()
        yield "First. "
        if self.block: await self.release.wait()
        if self.fail: raise RuntimeError("provider failure")
        yield "Second."
        self.audit({"stage": "text", "model": "test-text", "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}, "apiCalls": 1})

    async def speak_stream(self, tokens, emit):
        async for _ in tokens:
            emit(base64.b64encode(b"\x01\x00" * 240).decode())

    async def observe(self, instructions, image):
        self.frames.append((instructions, image))
        return '{"visible":true}'

    async def transcribe(self, pcm):
        self.pcm.append(pcm)
        if self.fail: raise RuntimeError("ASR failed")
        return "Cup on table"

    async def close(self): self.closed = True


class AlwaysSpeech:
    def is_speech(self, frame, rate): return frame != bytes(640)


def conversation(**kwargs):
    callback = RecordingCallback()
    result = ModularConversation(model="unused-omni", api_key="test", callback=callback,
                                 provider_factory=FakeProvider, vad=AlwaysSpeech(), **kwargs)
    result.connect()
    result.update_session(enable_input_audio_transcription=True, instructions="Only cited evidence")
    return result, callback


async def settle(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate(): await asyncio.sleep(.005)


def test_streaming_response_maps_ids_and_replays_context():
    async def run():
        c, callback = conversation()
        c.create_item({"id": "q", "type": "message", "role": "user", "content": [{"type": "input_text", "text": "where?"}]})
        c.create_item({"id": "q", "type": "message", "role": "user", "content": "duplicate"})
        c.create_item({"id": "receipt", "type": "function_call_output", "call_id": "tool1", "output": '{"source":"cup"}'})
        c.create_response(instructions="Answer from this context", output_modalities=["audio", "text"])
        await settle(lambda: any(e["type"] == "response.done" for e in callback.events))
        ids = {e.get("response_id", (e.get("response") or {}).get("id")) for e in callback.events if e["type"].startswith("response.")}
        assert len(ids) == 1
        assert sum(e["type"] == "response.audio.delta" for e in callback.events) == 2
        assert callback.events[-1]["response"]["status"] == "completed"
        assert callback.events[-1]["response"]["usage"]["total_tokens"] == 12
        assert [m["content"] for m in c.provider.messages[0]].count("where?") == 1
        assert "toolReceipt" in c.provider.messages[0][2]["content"]
        assert c.provider.messages[0][0]["content"] == "Only cited evidence"
        await c.aclose()
        assert c.provider.closed and not c.tasks
    asyncio.run(run())


@pytest.mark.parametrize("action", ["cancel", "fail", "close", "immediate-cancel", "before-start"])
def test_cancel_failure_and_close_never_emit_completed_or_late_audio(action):
    async def run():
        c, callback = conversation()
        c.provider.block = action != "fail"
        c.provider.fail = action == "fail"
        if action == "immediate-cancel":
            original = callback.on_event
            def event(value):
                original(value)
                if value["type"] == "response.created": c.cancel_response()
            callback.on_event = event
        c.create_response(instructions="answer", output_modalities=["audio"])
        if action == "before-start": c.cancel_response()
        if action not in {"immediate-cancel", "before-start"}:
            await c.provider.started.wait()
        if action == "close":
            await asyncio.to_thread(c.close)
        elif action == "cancel": c.cancel_response()
        await settle(lambda: not c.tasks)
        count = len(callback.events)
        c.provider.release.set()
        await asyncio.sleep(.01)
        assert not any(e["type"] == "response.done" for e in callback.events)
        assert len(callback.events) == count
        if action in {"immediate-cancel", "before-start"}:
            assert not c.provider.messages
            assert any(e["type"] == "response.cancelled" for e in callback.events)
        await c.aclose()
    asyncio.run(run())


def test_cancelling_output_does_not_cancel_asr():
    async def run():
        c, callback = conversation()
        c.provider.block = True
        c.create_response(instructions="answer", output_modalities=["audio"])
        await c.provider.started.wait()
        c.cancel_response()
        speech, silence = b"\x01\x00" * 320, bytes(640)
        for pcm in [speech] * 10 + [silence] * 33:
            c.append_audio(base64.b64encode(pcm).decode())
        await settle(lambda: any(e["type"] == "conversation.item.input_audio_transcription.completed" for e in callback.events))
        event = next(e for e in callback.events if e["type"] == "conversation.item.input_audio_transcription.completed")
        assert event["transcript"] == "Cup on table"
        assert c.provider.pcm
        assert not c.items  # Application owns transcript context insertion.
        await c.aclose()
    asyncio.run(run())


def test_asr_failure_releases_pending_segment():
    async def run():
        c, callback = conversation()
        c.provider.fail = True
        c._speech_start("segment", 0)
        c._speech_end("segment", 100, bytes(3200))
        await settle(lambda: any(e["type"] == "pipeline.error" for e in callback.events))
        dropped = next(e for e in callback.events if e["type"] == "conversation.item.input_audio_transcription.completed")
        assert dropped["item_id"] == "segment" and dropped["transcript"] == ""
        await c.aclose()
    asyncio.run(run())


def test_asr_queue_overflow_is_audited_and_does_not_leave_pending_input(monkeypatch):
    monkeypatch.setenv("EGOASSIST_MODULAR_ASR_QUEUE", "1")
    async def run():
        c, callback = conversation()
        c._speech_end("first", 100, bytes(3200))
        c._speech_end("overflow", 200, bytes(3200))
        assert any(e.get("reason") == "queue-full" and e["itemId"] == "overflow" for e in callback.events)
        assert any(e.get("item_id") == "overflow" and e.get("transcript") == "" for e in callback.events)
        await settle(lambda: not c.tasks)
        assert len(c.provider.pcm) == 1
        await c.aclose()
    asyncio.run(run())


def test_visual_session_never_processes_microphone_or_tts():
    async def run():
        callback = RecordingCallback()
        c = ModularConversation(model="vision", api_key="test", callback=callback, provider_factory=FakeProvider)
        c.update_session(enable_input_audio_transcription=False)
        c.append_audio(base64.b64encode(bytes(640)).decode())
        c.append_video("frame")
        c.create_response(instructions="inspect", output_modalities=["text"])
        await settle(lambda: any(e["type"] == "response.done" for e in callback.events))
        assert c.provider.frames == [("inspect", "frame")]
        assert not c.provider.messages and not c.provider.pcm
        assert not any(e["type"] == "response.audio.delta" for e in callback.events)
        await c.aclose()
    asyncio.run(run())


def test_vad_preserves_offsets_bounded_segments_and_arbitrary_chunking():
    starts, ends = [], []
    segmenter = SpeechSegments(lambda *a: starts.append(a), lambda *a: ends.append(a), max_seconds=1, vad=AlwaysSpeech())
    data = bytes(640) * 20 + (b"\x01\x00" * 320) * 120 + bytes(640) * 33
    for offset in range(0, len(data), 514): segmenter.feed(data[offset:offset + 514])
    assert starts and len(starts) == len(ends)
    assert starts[0][1] >= 0
    assert all(len(pcm) <= 32000 for _, _, pcm in ends)
    assert [e[0] for e in ends] == [s[0] for s in starts]
    assert len({s[0] for s in starts}) == len(starts)


def test_history_budget_is_bounded_and_reports_eviction(monkeypatch):
    monkeypatch.setenv("EGOASSIST_MODULAR_HISTORY_CHARS", "1024")
    async def run():
        c, callback = conversation()
        for i in range(10): c.create_item({"id": str(i), "type": "message", "role": "user", "content": "a" * 300})
        assert sum(len(row[1]["content"]) for row in c.items) <= 1024
        assert any(e.get("status") == "evicted-oldest-item" for e in callback.events)
        with pytest.raises(ValueError):
            c.create_item({"type": "message", "role": "user", "content": "a" * 2048})
        await c.aclose()
    asyncio.run(run())


def test_modular_bridge_uses_common_scheduler_and_isolated_lanes(monkeypatch):
    monkeypatch.setenv("EGOASSIST_REALTIME_BACKEND", "modular")
    import egoassist.realtime.modular as module
    monkeypatch.setattr(module, "ModularConversation", partial(ModularConversation, provider_factory=FakeProvider, vad=AlwaysSpeech()))
    async def run():
        emitted = []
        async def emit(event): emitted.append(event)
        bridge = OmniBrowserBridge("test", "omni-name-not-used", "Ethan", "context", emit, lambda text: None)
        await bridge.connect()
        assert bridge.connected
        assert bridge.provider_lanes_snapshot()["dialogue"]["backend"] == "modular"
        assert bridge.perception.model == "qwen3-vl-flash"
        bridge.request_response("answer")
        await settle(lambda: any(e["type"] == "response_status" and e["status"] == "completed" for e in emitted))
        assert any(e["type"] == "audio" for e in emitted)
        assert not bridge.scheduler_snapshot()["activeResponseIds"]
        await bridge.close()
        assert bridge.conversation.provider.closed
    asyncio.run(run())


def test_invalid_backend_fails_explicitly(monkeypatch):
    monkeypatch.setenv("EGOASSIST_REALTIME_BACKEND", "typo")
    async def run():
        with pytest.raises(ValueError, match="omni or modular"):
            OmniBrowserBridge("test", "model", "voice", "context", None, None)
    asyncio.run(run())


def test_reconnect_replays_durable_context_and_cancels_old_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("EGOASSIST_REALTIME_BACKEND", "modular")
    import egoassist.realtime.modular as module
    from egoassist.realtime import ContextHarness, ContextSettings, SessionJournal
    monkeypatch.setattr(module, "ModularConversation", partial(ModularConversation, provider_factory=FakeProvider, vad=AlwaysSpeech()))
    async def run():
        async def emit(event): pass
        bridge = OmniBrowserBridge("test", "unused", "Ethan", "context", emit, lambda text: None)
        harness = ContextHarness(SessionJournal(tmp_path / "events.jsonl", session_id="s"), ContextSettings())
        harness.record_user("remember the source", source="test")
        bridge.set_context_harness(harness)
        await bridge.connect()
        old = bridge.conversation
        await bridge._recover_dialogue("test-reconnect")
        assert bridge.conversation is not old and old.provider.closed
        assert any("remember the source" in item[1]["content"] for item in bridge.conversation.items)
        await bridge.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["truncated", "error", "disconnect"])
def test_text_sse_failures_propagate_and_are_audited(failure):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    async def run():
        async def handler(request):
            body = await request.json()
            assert body["parameters"]["incremental_output"]
            assert request.headers["X-DashScope-SSE"] == "enable"
            event = {"output": {"choices": [{"message": {"content": "part"}, "finish_reason": "length" if failure == "truncated" else "null"}]}}
            if failure == "error": event = {"code": "provider-failed"}
            return web.Response(text="data: " + json.dumps(event) + "\n\n", content_type="text/event-stream")
        app = web.Application()
        app.router.add_post("/stream", handler)
        async with TestServer(app) as server:
            audit = []
            provider = ModularProvider("test", settings(), audit.append)
            provider.url = lambda endpoint: str(server.make_url("/stream"))
            try:
                with pytest.raises((ValueError, RuntimeError)):
                    async for _ in provider.text_stream([{"role": "user", "content": "q"}]): pass
                assert audit[-1]["status"] == "failed"
            finally: await provider.close()
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_tts_prefetch_is_bounded_waits_for_handshake_and_cancels(monkeypatch, cancel):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def run():
        token_started, stream_closed, allow_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        produced, sent, audio, audit = [], [], [], []

        async def tokens():
            try:
                for token in ("first", "second"):
                    produced.append(token)
                    token_started.set()
                    yield token
            finally:
                stream_closed.set()

        async def tts(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            assert (await ws.receive_json())["type"] == "session.update"
            await allow_ready.wait()
            if cancel:
                await ws.close()
                return ws
            await ws.send_json({"type": "session.updated"})
            while True:
                event = await ws.receive_json()
                sent.append(event)
                if event["type"] == "session.finish":
                    break
            await ws.send_json({"type": "response.audio.delta", "delta": base64.b64encode(bytes(4)).decode()})
            await ws.send_json({"type": "session.finished"})
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/tts", tts)
        async with TestServer(app) as server:
            monkeypatch.setattr("egoassist.realtime.modular.TTS_URL", str(server.make_url("/tts")))
            provider = ModularProvider("test", {**settings(), "ttsPrefetch": True}, audit.append)
            task = asyncio.create_task(provider.speak_stream(tokens(), audio.append))
            try:
                await asyncio.wait_for(token_started.wait(), 2)
                await asyncio.sleep(.03)
                assert produced == ["first"] and not sent and not audio
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert stream_closed.is_set()
                    assert audit[-1]["status"] == "cancelled"
                    assert not audio
                else:
                    allow_ready.set()
                    await asyncio.wait_for(task, 2)
                    assert [e["text"] for e in sent if e["type"] == "input_text_buffer.append"] == ["first", "second"]
                    assert len(audio) == 1 and stream_closed.is_set()
                    assert audit[-1]["firstTextReadyMs"] < audit[-1]["sessionReadyMs"]
                    assert audit[-1]["status"] == "completed"
            finally:
                allow_ready.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await provider.close()
    asyncio.run(run())
