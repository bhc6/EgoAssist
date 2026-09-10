"""Concurrent observation must not overwrite question-time evidence."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from egoassist.realtime.context_harness import SessionJournal
from egoassist.realtime.omni import OmniBrowserBridge
from egoassist.realtime.scene import SharedScene


def test_task_json_fence_and_false_strings():
    from egoassist.realtime.perception import parse_perception_hypothesis
    value = parse_perception_hypothesis('```json\n{"checkpoint_id":"unknown","completed":"false","unrelated":"false","confidence":0,"summary":"table"}\n```')
    assert not value.completed and not value.unrelated


def test_ambient_continues_while_dialogue_and_task_lane_busy():
    bridge = OmniBrowserBridge.__new__(OmniBrowserBridge)
    bridge.closed = False
    bridge.last_ambient_check = 0
    bridge._response_pending = True
    bridge.perception = SimpleNamespace(active=True)
    calls = []
    bridge.ambient_perception = SimpleNamespace(request=lambda image, context: calls.append((image, context)))
    now = datetime.now(timezone.utc)
    bridge._latest_perception_frame = {"frameId": "one", "capturedAt": now, "image": "image"}
    bridge._ambient_context_provider = lambda frame, captured: {"frame_id": frame, "frame_captured_at": captured}
    bridge.maybe_observe_ambient(2)
    bridge.maybe_observe_ambient(2)
    assert len(calls) == 1
    assert calls[0][1]["frame_id"] == "one"
    assert bridge._response_pending and bridge.perception.active


def test_observation_snapshot_excludes_future_and_survives_restart(tmp_path):
    journal = SessionJournal(tmp_path / "session.jsonl", session_id="session")
    scene = SharedScene(journal, "")
    now = datetime.now(timezone.utc)

    def observe(frame_id, at):
        row = journal.append("ambient_observation", {"status": "accepted", "observation_id": frame_id,
            "evidence": {"capturedAt": at.isoformat(), "visibleEvidence": [frame_id], "uncertainties": []}})
        scene.ingest_observation(row)
        return row

    observe("past", now - timedelta(seconds=2))
    observe("future", now + timedelta(minutes=1))
    first = scene.capture("question", explicit=True)
    assert first["observationContext"]["visibleEvidence"] == ["past"]
    observe("later-result", now - timedelta(seconds=1))
    assert scene.capture("question", explicit=True) == first
    restarted = SharedScene(journal, "")
    context = asyncio.run(restarted.context("question"))
    assert context["observationContext"]["visibleEvidence"] == ["past"]
    assert scene.observation_context(now.isoformat())["visibleEvidence"] == ["later-result"]


def test_observation_references_exclude_assistant_messages(tmp_path):
    from egoassist.runtime import ThreeLaneRuntime
    journal = SessionJournal(tmp_path / "session.jsonl", session_id="session")
    user = journal.append("message", {"message": {"role": "user", "content": "Watch my keys"}})
    journal.append("message", {"message": {"role": "assistant", "content": "Keys are on the table"}})
    timeline = SimpleNamespace(cards=lambda **kw: [], pending_draft=lambda **kw: None)
    value = ThreeLaneRuntime(journal, "user").observation_references(timeline)
    assert [r["sourceId"] for r in value["userMessages"]] == [user["id"]]
    assert "Keys are on the table" not in str(value)


def test_three_lanes_keep_memory_and_answers_independent_of_observation(tmp_path):
    from egoassist.runtime import ThreeLaneRuntime
    journal = SessionJournal(tmp_path / "session.jsonl", session_id="session")
    journal.append("memory.recovery.job", {"turnId": "one", "status": "queued"})
    journal.append("memory.recovery.job", {"turnId": "one", "status": "completed"})
    journal.append("insight.job", {"jobId": "report", "status": "running", "phase": "model"})
    journal.append("answer.job", {"turnId": "two", "status": "running"})
    framework = ThreeLaneRuntime(journal, "user")
    snapshot = framework.snapshot(observation=None, scheduler={}, classifying=2)
    assert snapshot["observation"]["enabled"] is False
    assert snapshot["memory"]["jobs"]["completed"] == 1
    assert snapshot["memory"]["jobs"]["queued"] == 0
    assert snapshot["memory"]["jobs"]["running"] == 1
    assert snapshot["interaction"]["answers"]["running"] == 1
    assert snapshot["interaction"]["classifying"] == 2
    restored = ThreeLaneRuntime(SessionJournal(journal.path), "user").snapshot(observation=None, scheduler={})
    assert restored["memory"] == snapshot["memory"]
