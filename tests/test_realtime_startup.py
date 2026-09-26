import asyncio
from collections import deque
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from egoassist.core.journal import SessionJournal
from egoassist.realtime.omni import OmniBrowserBridge
from egoassist.realtime.turn_coordinator import TurnCoordinator


def test_speech_endpoint_waits_through_pause_without_delaying_barge_in(monkeypatch):
    monkeypatch.delenv('EGOASSIST_SPEECH_END_SILENCE_MS', raising=False)
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge.voice, bridge.instructions = 'Ethan', 'test'
    bridge.modalities = SimpleNamespace(AUDIO='audio', TEXT='text')
    bridge.interaction_managed = True
    bridge._trace = lambda *args: None
    calls = []
    conversation = SimpleNamespace(update_session=lambda **kwargs: calls.append(kwargs))
    bridge._configure_dialogue(conversation)
    assert calls[-1]['turn_detection_silence_duration_ms'] == 1000
    assert calls[-1]['output_modalities'] == ['audio', 'text']
    assert calls[-1]['turn_detection_param'] == {'create_response': False, 'interrupt_response': False}
    bridge._configure_dialogue(conversation)
    assert len(calls) == 1
    monkeypatch.setenv('EGOASSIST_SPEECH_END_SILENCE_MS', '1600')
    bridge._configure_dialogue(conversation)
    assert calls[-1]['turn_detection_silence_duration_ms'] == 1600
    monkeypatch.setenv('EGOASSIST_SPEECH_END_SILENCE_MS', 'nan')
    with pytest.raises(ValueError): bridge._configure_dialogue(conversation)


@pytest.mark.parametrize("close_during_replay", [False, True])
def test_history_replay_keeps_loop_responsive(monkeypatch, close_during_replay):
    async def scenario():
        bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
        bridge._blocked_failure = None
        bridge.closed = bridge.connected = False
        bridge._dialogue_generation = 0
        bridge._reconnect_lock = asyncio.Lock()
        bridge._buffered_audio = deque()
        bridge._reset_scheduler_after_reconnect = lambda: None
        bridge._trace = lambda *args: None
        closed = []
        conversation = SimpleNamespace(close=lambda: closed.append(True))
        bridge._new_dialogue_conversation = lambda _: conversation
        bridge._configure_dialogue = lambda _: None
        stages = []

        async def emit(payload):
            stages.append(payload)

        bridge.emit = emit
        entered, release = threading.Event(), threading.Event()

        def replay(_):
            entered.set()
            assert release.wait(2), "history replay blocked the event loop"

        bridge._replay_context = replay
        monkeypatch.setattr("egoassist.realtime.omni._connect_with_resolved_ip", lambda _: None)
        task = asyncio.create_task(bridge._connect_dialogue())
        try:
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            assert not task.done()
            assert not bridge.connected
            bridge.closed = close_during_replay
        finally:
            release.set()
        await task
        assert bridge.connected is not close_during_replay
        assert bool(closed) is close_during_replay
        assert [row["stage"] for row in stages] == ["provider-connect", "context-restore"]

    asyncio.run(scenario())


def test_progress_survives_browser_disconnect(tmp_path):
    journal = SessionJournal(tmp_path / "progress.jsonl")
    coordinator = TurnCoordinator.__new__(TurnCoordinator)
    coordinator.harness = SimpleNamespace(journal=journal)

    async def send_json(_):
        raise ConnectionResetError("closing transport")

    coordinator.ws = SimpleNamespace(closed=False, send_json=send_json)
    asyncio.run(coordinator.turn_progress("turn-1", "classifying"))
    assert journal.entries(types={"turn.progress"})[-1]["phase"] == "classifying"


def test_serving_error_retains_correlation_without_claiming_answer_success(monkeypatch):
    monkeypatch.setenv("EGOASSIST_REALTIME_BACKEND", "omni")

    async def scenario():
        async def emit(_):
            pass

        bridge = OmniBrowserBridge("test", "test", "Ethan", "test", emit, lambda _: None)
        events, traces = [], []
        bridge._schedule = events.append
        bridge._trace = lambda kind, payload: traces.append((kind, payload))
        bridge._Callback(bridge._dialogue_generation).on_event({
            "event_id": "event_serving_failure", "type": "error",
            "error": {"code": "COMMON_ERROR", "message":
                      "<50002> InternalError.Algo.ModelServingError: Internal Error calling model processing."},
        })
        assert len(events) == 1 and events[0]["type"] == "error"
        assert events[0]["retryable"] is True
        assert events[0]["eventId"] == "event_serving_failure"
        assert events[0]["code"] == "COMMON_ERROR"
        assert "请重试" in events[0]["message"]
        assert traces[0][1]["eventId"] == "event_serving_failure"
        assert "50002" in traces[0][1]["message"]
        await bridge.close()

    asyncio.run(scenario())


def test_browser_waits_for_history_and_preserves_startup_error():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for console tests")
    source = (Path(__file__).parents[1] / "egoassist/console/app.js").read_text(encoding="utf-8")
    source = source[source.index("async function connectRealtimeSession("):source.index("function stopAudioCapture()")]
    program = r'''
const assert = require('node:assert/strict');
let selectedSessionId = 'case', connectionGeneration = 1, socket, stateRefreshPendingAt;
let stream = null, providerReady = false, currentUser = null, currentTurnId;
let state, interactionTransport = 'websocket-fallback';
const stoppedSpeechResponses = new Set(), timers = new Map(), elements = new Map();
const location = {protocol: 'http:', host: 'localhost'};
const $ = key => { if (!elements.has(key)) elements.set(key, {}); return elements.get(key); };
const setTimeout = (fn, ms) => { const id = {}; timers.set(id, {fn, ms}); return id; };
const clearTimeout = id => timers.delete(id);
const refreshSessionList = async () => {};
const sendMediaConfiguration = () => {};
const stopPlayback = () => {};
const turnOutbox = () => ({entries: () => []});
class WebSocket {
  constructor() { this.readyState = 1; }
  close(code, reason) { this.closed = {code, reason}; }
  message(payload) { this.onmessage({data: JSON.stringify(payload)}); }
}
''' + source + r'''
(async () => {
  let settled = false;
  const connection = connectRealtimeSession(1, false, 0);
  connection.then(() => {settled = true;});
  socket.onopen();
  socket.message({type: 'connection_progress', stage: 'context-restore'});
  assert.match($('#duplexState').textContent, /恢复历史/);
  assert.equal([...timers.values()][0].ms, 60000);
  for (const timer of timers.values()) if (timer.ms <= 23000) timer.fn();
  await Promise.resolve();
  assert.equal(settled, false);
  assert.equal(socket.closed, undefined);
  socket.message({type: 'ready', lanes: {dialogue: {connected: true}}});
  await connection;
  assert.equal(providerReady, true);
  assert.equal(timers.size, 0);

  const failed = connectRealtimeSession(1, false, 0);
  socket.message({type: 'error', message: 'provider rejected credentials'});
  await assert.rejects(failed, /provider rejected credentials/);
  assert.equal(socket.closed.reason, 'startup-error');
  assert.equal(timers.size, 0);

  const stalled = connectRealtimeSession(1, false, 0);
  socket.message({type: 'connection_progress', stage: 'context-restore'});
  [...timers.values()][0].fn();
  await assert.rejects(stalled, /历史上下文恢复.*60 秒/);
  assert.equal(socket.closed.reason, 'ready-timeout');
  assert.equal(timers.size, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run([node, "-"], input=program, text=True, encoding="utf-8", check=True, timeout=10)
