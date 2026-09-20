import asyncio
import base64
import io
from egoassist.realtime.interaction_window import select_frames

import pytest
from PIL import Image

from egoassist.realtime.context_harness import SessionJournal
from egoassist.realtime.scene import SharedScene, restrict_unverified_speaker
from egoassist.realtime.speakers import SpeakerRegistry


def make_scene(tmp_path, session="first"):
    return SharedScene(SessionJournal(tmp_path / (session + ".jsonl"), session_id=session), "")


def frame(scene, color):
    out = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(out, format="JPEG")
    scene.append_frame(base64.b64encode(out.getvalue()).decode())


def test_question_frame_is_immutable_and_context_does_not_wait_for_vision(tmp_path):
    scene = make_scene(tmp_path)
    frame(scene, "red")
    saved = scene.capture("question", explicit=True)["frame"]
    frame(scene, "green")
    context = asyncio.run(scene.context("question"))
    assert context["visual"]["frame"] == saved
    assert context["visual"]["status"] == "frame-available"
    restarted = make_scene(tmp_path)
    assert restarted.turns["question"]["frame"] == saved
    assert restarted.media(saved) == scene.media(saved)
    with pytest.raises(FileNotFoundError):
        make_scene(tmp_path, "other").media(saved)
    (scene.root / saved["assetId"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        scene.media(saved)


def test_interaction_window_keeps_before_and_after_samples_and_excludes_future(tmp_path):
    scene = make_scene(tmp_path)
    now = 100.0
    # Use small in-memory frame placeholders; selection only needs timing.
    frames = [{"frameId": str(i), "monotonic": now + i, "device": {}, "data": b"x"}
              for i in range(-12, 3)]
    selected, window = select_frames(frames, end=now, start=now - 2, received_before=now)
    assert selected and all(at <= now for at, _ in selected)
    assert window["anchor"] == "speech-end"
    assert window["availableFrames"] == 13
    assert window["coverageMs"] == 12000
    assert not window["gaps"]


def test_audio_capture_clock_excludes_frames_received_during_delayed_asr(tmp_path, monkeypatch):
    from unittest.mock import patch
    scene = make_scene(tmp_path)
    with patch('egoassist.realtime.scene.time.monotonic', return_value=100):
        frame(scene, 'red')
    scene.frames[-1]['device'] = {'clockId': 'camera', 'monotonicMs': 9000}
    with patch('egoassist.realtime.scene.time.monotonic', return_value=102):
        frame(scene, 'blue')
    scene.frames[-1]['device'] = {'clockId': 'camera', 'monotonicMs': 10500}
    scene.append_audio(base64.b64encode(bytes(32000)).decode(), 1,
                       metadata={'clockId': 'camera', 'startMonotonicMs': 9000, 'endMonotonicMs': 10000})
    row = scene.capture('delayed', segment={'audioStartMs': 0, 'audioEndMs': 1000, 'generation': 1})
    window = row['interactionWindow']
    assert window['syncBasis'] == 'client-monotonic-estimate'
    assert window['frames'] and all(f['device']['monotonicMs'] <= 10000 for f in window['frames'])
    assert row['frame']['device']['monotonicMs'] == 9000
    # Original question stays frozen when a later frame arrives and after restart.
    frame(scene, 'green')
    assert scene.capture('delayed')['interactionWindow'] == window
    assert make_scene(tmp_path).turns['delayed']['interactionWindow'] == window


def test_dense_window_keeps_previous_second_and_has_bounded_samples():
    frames = [{'monotonic': i / 4, 'device': {}} for i in range(60)]
    selected, window = select_frames(frames, end=12, received_before=15)
    assert len(selected) <= 16 and window['sampled']
    assert selected[-1][0] == 12
    assert {11, 11.25, 11.5, 11.75, 12}.issubset({at for at, _ in selected})


def test_capture_window_cannot_join_device_clock_generations():
    frames = [{'monotonic': 20, 'device': {'clockId': c, 'monotonicMs': t}}
              for c, t in [('old', 1000), ('new', 1000), ('new', 3000)]]
    selected, _ = select_frames(frames, end=2, clock_id='new', received_before=30)
    assert len(selected) == 1 and selected[0][1]['device']['clockId'] == 'new'


def test_wrong_audio_epoch_cannot_borrow_newer_frame(tmp_path):
    scene = make_scene(tmp_path)
    frame(scene, "red")
    scene.append_audio(base64.b64encode(b"\0" * 32000).decode(), 2)
    row = scene.capture("old", segment={"audioStartMs": 0, "audioEndMs": 900, "generation": 1})
    assert row["audio"] is None and row["frame"] is None
    assert not row["speaker"]["wearer"]


def test_snapshot_preserves_audio_frame_evidence_after_restore(tmp_path):
    scene = make_scene(tmp_path)
    frame(scene, "red")
    # The frame must precede the 900 ms endpoint, not arrive with the 1000 ms chunk.
    scene.frames[-1]['monotonic'] -= .2
    scene.append_audio(base64.b64encode(bytes(32000)).decode(), 1)
    captured = scene.capture("speech", segment={"audioStartMs": 0, "audioEndMs": 900, "generation": 1})
    assert captured["audio"] and captured["synchronizedFrames"]
    for projection in (scene.snapshot(), make_scene(tmp_path).snapshot()):
        turn = projection["utterances"][0]
        assert turn["audio"] == captured["audio"]
        assert turn["synchronizedFrames"] == captured["synchronizedFrames"]
        assert turn["avAssociation"]["status"] == "unlinked"


def test_replayed_prefix_offsets_provider_audio_clock(tmp_path):
    scene = make_scene(tmp_path)
    scene.append_audio(base64.b64encode(bytes(32000)).decode(), "connection2:1", sample_offset=16000)
    absent = scene.capture("replayed", segment={"audioStartMs": 0, "audioEndMs": 900, "generation": "connection2:1"})
    present = scene.capture("new", segment={"audioStartMs": 1000, "audioEndMs": 1900, "generation": "connection2:1"})
    assert absent["audio"] is None
    assert present["audio"] is not None


def test_out_of_order_asr_uses_provider_item_id():
    import threading
    from collections import deque
    from egoassist.realtime.omni import OmniBrowserBridge
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._media_lock = threading.Lock()
    bridge._dialogue_generation = 1
    bridge._pending_input_segments = deque()
    bridge._trace = lambda *args, **kwargs: None
    bridge._input_segment_started({"item_id": "first", "audio_start_ms": 0})
    bridge._input_segment_stopped({"audio_end_ms": 1000})
    bridge._input_segment_started({"item_id": "second", "audio_start_ms": 1100})
    bridge._input_segment_stopped({"audio_end_ms": 2000})
    assert not bridge._input_segment_transcribed("ambiguous")
    assert bridge._input_segment_transcribed("second words", "second")["audioStartMs"] == 1100
    assert bridge._input_segment_transcribed("first words", "first")["audioStartMs"] == 0
    bridge._input_segment_started({"item_id": "stale", "audio_start_ms": 3000})
    bridge._drop_open_input_segments("reconnect")
    assert not bridge._input_segment_contexts


def test_vad_stop_rebinds_provisional_id_without_leaving_speech_pending():
    import threading
    from collections import deque
    from egoassist.realtime.omni import OmniBrowserBridge
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._media_lock = threading.Lock()
    bridge._dialogue_generation = 1
    bridge._pending_input_segments = deque()
    bridge._trace = lambda *args, **kwargs: None
    bridge._input_segment_started({'item_id': 'provisional', 'audio_start_ms': 100})
    bridge._input_segment_stopped({'item_id': 'committed', 'audio_end_ms': 1800})
    segment = bridge._input_segment_transcribed('hello', 'committed')
    assert segment['audioStartMs'] == 100 and segment['audioEndMs'] == 1800
    assert segment['startProviderItemId'] == 'provisional'
    assert not bridge.has_unresolved_user_input()


def test_late_stop_for_older_item_does_not_close_new_speech():
    import threading
    from collections import deque
    from egoassist.realtime.omni import OmniBrowserBridge
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge._media_lock = threading.Lock()
    bridge._dialogue_generation = 1
    bridge._pending_input_segments = deque()
    bridge._trace = lambda *args, **kwargs: None
    first = bridge._input_segment_started({'item_id': 'first', 'audio_start_ms': 0})
    second = bridge._input_segment_started({'item_id': 'second', 'audio_start_ms': 1200})
    bridge._input_segment_stopped({'item_id': 'first', 'audio_end_ms': 1000})
    assert bridge._active_input_segment_id == second
    assert bridge._input_segment_contexts[first]['audioEndMs'] == 1000
    assert 'audioEndMs' not in bridge._input_segment_contexts[second]


def test_different_clusters_and_omitted_speech_never_become_whole_wearer(tmp_path):
    registry = SpeakerRegistry(make_scene(tmp_path).journal)
    analysis = {"segments": [{"cluster": 0, "embedding": [1., 0.], "startMs": 0, "endMs": 1000}]}
    first = registry.match(analysis)
    registry.confirm(first["speakerId"], name="Wearer", wearer=True)
    assert registry.match(analysis)["wearer"]
    partial = registry.match({**analysis, "omittedSegments": 1})
    assert not partial["wearer"] and partial["speakerId"] is None
    mixed = registry.match({"segments": analysis["segments"] + [
        {"cluster": 1, "embedding": [1., 0.], "startMs": 1100, "endMs": 2200}]})
    assert len({s["speakerId"] for s in mixed["segments"]}) == 2
    assert not mixed["wearer"]


def test_unknown_speech_preserves_commands_without_claiming_identity():
    result = restrict_unverified_speaker({"memoryText": "personal fact", "revision": {"supersedesTurnId": "a"},
        "interaction": {"action": "cancel"}, "taskControl": "start", "intents": [{"kind": "task"}]})
    assert result["memoryText"] == "personal fact" and result["revision"]["supersedesTurnId"] == "a"
    assert result["taskControl"] == "start" and result["intents"]
    assert result["interaction"]["action"] == "cancel"
    assert result["speakerAttribution"] == "unconfirmed"


def test_scene_media_http_is_owner_scoped_and_history_is_complete(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from egoassist import BUILTIN_SKILLS_ROOT
    from egoassist.realtime.work_ledger import CaseWorkLedger
    from run_egoassist import create_app

    async def scenario():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / "runtime.json"), "test", "Ethan")
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/scene/media")).status == 401
            await client.post("/api/auth/register", json={"username": "scene-owner", "password": "scene-test-pass-123"})
            sid = (await (await client.post("/api/sessions")).json())["session"]["id"]
            await client.get("/api/state", params={"session": sid})
            path = next(tmp_path.glob(f"users/*/sessions/{sid}.jsonl"))
            journal = SessionJournal(path, session_id=sid)
            scene = SharedScene(journal, "")
            frame(scene, "red")
            ref = scene.capture("image", explicit=True)["frame"]
            response = await client.get("/api/scene/media", params={"session": sid, "asset": ref["assetId"]})
            assert response.status == 200 and await response.read() == scene.media(ref)
            assert (await client.get("/api/scene/media", params={"session": sid, "asset": "../private"})).status == 400
            ledger = CaseWorkLedger(journal)
            for index in range(25):
                ledger.capture_turn(str(index), turn_id="turn-" + str(index), source="test")
            state = await (await client.get("/api/state", params={"session": sid})).json()
            assert len(state["workLedger"]["turnInbox"]) == 25
            await client.post("/api/auth/register", json={"username": "scene-other", "password": "scene-test-pass-456"})
            assert (await client.get("/api/scene/media", params={"session": sid, "asset": ref["assetId"]})).status in {400, 403}
    asyncio.run(scenario())
