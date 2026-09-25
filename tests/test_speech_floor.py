"""Conversation timing invariants across real application queues (fake providers)."""
import asyncio
import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.realtime.interaction import InteractionState
from egoassist.realtime.omni import OmniBrowserBridge
from run_egoassist import create_app
from test_realtime_api import install_queue_test_bridge, register_client, semantic_result
import run_egoassist


def test_managed_vad_observes_without_stop_or_cancel():
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge.interaction_managed = True
    traces = []
    bridge._trace = lambda *args: traces.append(args)
    # No scheduler or response lock: the managed path must not touch playback.
    for trigger in ("device-vad", "provider-speech-started", "transcript-fallback"):
        assert bridge.handle_user_speech_started(trigger=trigger) == "listening"
    assert len(traces) == 3


def test_stopped_asr_timeout_is_audited_but_active_speech_never_expires():
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._media_lock = threading.Lock()
    bridge._input_speech_active = True
    bridge._pending_input_segments = deque(["lost", "speaking"])
    bridge._input_segment_contexts = {"lost": {"stoppedAtMonotonic": time.monotonic() - 31}, "speaking": {}}
    traces = []
    bridge._trace = lambda *args: traces.append(args)
    assert bridge.has_unresolved_user_input()
    assert list(bridge._pending_input_segments) == ["speaking"]
    assert traces[0][1]["reason"] == "stopped-asr-deadline"
    bridge._input_speech_active = False
    bridge._pending_input_segments.clear()
    assert not bridge.has_unresolved_user_input()


def test_keep_preserves_quiet_reply_grant_and_wait_is_not_permanent_quiet(tmp_path):
    state = InteractionState(SessionJournal(tmp_path / "floor.jsonl"))
    state.apply({"audience": "assistant", "action": "listen", "output": "silent"}, "quiet")
    state.apply({"audience": "assistant", "action": "reply", "output": "speech"}, "question")
    state.apply({"audience": "assistant", "action": "keep", "output": "silent"}, "ack")
    assert state.allows({"turnId": "question"})
    assert not state.allows({}, "proactive-utterance")
    state.apply({"audience": "assistant", "action": "wait", "output": "silent", "quiet": {"mode": "clear"}}, "wait")
    assert state.paused and not state.snapshot()["quiet"]
    state.apply({"audience": "assistant", "action": "reply", "output": "speech"}, "finished")
    assert not state.paused and state.allows({"turnId": "finished"})


@pytest.mark.parametrize("aside", ["keep", "other", "uncertain"])
@pytest.mark.parametrize("late_question", [False, True])
def test_aside_preserves_pending_or_playing_answer(tmp_path, monkeypatch, aside, late_question):
    instances = install_queue_test_bridge(monkeypatch)

    async def scenario():
        first_started, release = asyncio.Event(), asyncio.Event()
        async def classify(_self, text, catalog):
            if text == "question":
                first_started.set()
                if late_question:
                    await release.wait()
            result = semantic_result(text)
            result["interaction"] = ({"audience": "assistant", "action": "reply", "output": "speech"}
                if text == "question" else {"audience": "other" if aside == "other" else "uncertain" if aside == "uncertain" else "assistant",
                    "action": "listen" if aside == "other" else aside, "output": "silent"})
            return result
        monkeypatch.setattr(run_egoassist.SemanticTurnRouter, "classify", classify)
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "runtime.json"), "test", "Ethan")
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
            ws = await client.ws_connect("/ws/realtime?session=" + sid)
            events = []
            async def until(predicate):
                async with asyncio.timeout(3):
                    while True:
                        event = await ws.receive_json()
                        events.append(event)
                        if predicate(event):
                            return event
            await until(lambda e: e["type"] == "state")
            await ws.send_json({"type": "control", "transcript": "question", "clientRequestId": "question"})
            await first_started.wait()
            if not late_question:
                await asyncio.wait_for(instances[0].request_event.wait(), 3)
            count = len(instances[0].cancellations)
            # Exercise the microphone ingress separately from typed question.
            instances[0].on_transcript("aside")
            await until(lambda e: e["type"] == "speech_control" and e["action"] == "keep")
            assert len(instances[0].cancellations) == count
            release.set()
            await asyncio.wait_for(instances[0].request_event.wait(), 3)
            assert len(instances[0].requests) == 1
            await instances[0].emit({"type": "audio", "audio": "AA==", "responseId": "answer",
                "purpose": "conversation", "origin": {"turnId": "question", "generation": 1}})
            assert (await until(lambda e: e["type"] == "audio"))["origin"]["turnId"] == "question"
            assert [e["turnId"] for e in events if e["type"] == "speech_floor"] == ["question"]
            state = await (await client.get("/api/state?session=" + sid)).json()
            assert not state["interaction"]["quiet"] and not state["interaction"]["paused"]
            await ws.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("queued", [False, True])
def test_admission_distinguishes_queued_audio_from_rejection(queued):
    from egoassist.realtime.turn_coordinator import TurnCoordinator
    async def scenario():
        token = object()
        coordinator = TurnCoordinator.__new__(TurnCoordinator)
        coordinator.owner = SimpleNamespace(socket_token=token, epoch=1, output_generation=1,
            speech_floor_sequence=1, pending_semantic_sequences=set(), mutation_lock=asyncio.Lock())
        coordinator.socket_token, coordinator.connection_epoch = token, 1
        bridge = SimpleNamespace(has_unresolved_user_input=lambda: False,
            scheduler_snapshot=lambda: {"queued": [{"origin": {"turnId": "q", "generation": 1}}] if queued else []})
        coordinator.bridge = lambda: bridge
        coordinator.interaction_state = SimpleNamespace(allows=lambda *args: True)
        coordinator.request_bridge_response = lambda *args, **kwargs: False
        assert await coordinator.admit_foreground_response(turn_id="q", turn_sequence=1, generation=1,
            instructions="answer", priority=100, interrupt=False, speech_policy="immediate",
            reason="test", max_protection_ms=2500, origin={}) is queued
    asyncio.run(scenario())


def test_new_semantic_stop_cancels_gap_waiter_then_next_question_speaks(tmp_path, monkeypatch):
    instances = install_queue_test_bridge(monkeypatch)
    async def classify(_self, text, catalog):
        result = semantic_result(text)
        result["interaction"] = {"audience": "assistant", "action": "wait" if text == "wait" else "reply",
                                 "output": "silent" if text == "wait" else "speech"}
        return result
    monkeypatch.setattr(run_egoassist.SemanticTurnRouter, "classify", classify)
    async def scenario():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "runtime.json"), "test", "Ethan")
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
            ws = await client.ws_connect("/ws/realtime?session=" + sid)
            async def until(kind, **fields):
                async with asyncio.timeout(3):
                    while True:
                        e = await ws.receive_json()
                        if e["type"] == kind and all(e.get(k) == v for k,v in fields.items()): return e
            await until("state")
            instances[0].unresolved_input = True
            for tid in ("question", "wait"):
                await ws.send_json({"type": "control", "transcript": tid, "clientRequestId": tid})
                await until("interaction_status", turnId=tid, status="waiting-speech-gap") if tid == "question" else await until("speech_control", turnId=tid)
            instances[0].unresolved_input = False
            await asyncio.sleep(.12)
            assert not instances[0].requests
            await ws.send_json({"type": "control", "transcript": "finished", "clientRequestId": "finished"})
            await asyncio.wait_for(instances[0].request_event.wait(), 3)
            assert len(instances[0].requests) == 1
            assert "finished" in instances[0].requests[0][0]
            await ws.close()
    asyncio.run(scenario())
