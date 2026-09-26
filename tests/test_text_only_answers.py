import asyncio
from functools import partial

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from egoassist.core.journal import SessionJournal
from egoassist.realtime.context_harness import ContextHarness
from egoassist.realtime.modular import ModularConversation
from egoassist.realtime.omni import OmniBrowserBridge
from egoassist.product.answer_jobs import AnswerJobs
from egoassist.realtime.work_ledger import CaseWorkLedger
from egoassist.realtime.interaction import InteractionState
from egoassist.realtime.interaction_controller import apply_interaction
from test_modular_realtime import FakeProvider, settle


class CloudConversation:
    def __init__(self):
        self.created, self.cancelled = [], 0

    def create_response(self, **kwargs):
        self.created.append(kwargs)

    def cancel_response(self):
        self.cancelled += 1

    def close(self):
        pass


@pytest.mark.parametrize("action", ["complete", "cancel", "close", "microphone", "tts-down"])
def test_explicit_text_uses_shared_context_without_audio_input(tmp_path, monkeypatch, action):
    monkeypatch.setenv("EGOASSIST_REALTIME_BACKEND", "omni")

    def provider(*args):
        result = FakeProvider(*args)
        result.block = action in {"cancel", "close"}
        if action == "tts-down":
            async def unavailable(*args):
                raise ConnectionError("TTS offline")
            result.speak_stream = unavailable
        return result

    monkeypatch.setattr("egoassist.realtime.modular.ModularConversation",
                        partial(ModularConversation, provider_factory=provider))

    async def scenario():
        events = []

        async def emit(payload):
            events.append(payload)

        bridge = OmniBrowserBridge("test", "test", "Ethan", "shared instructions", emit, lambda _: None)
        bridge.connected = True
        cloud = bridge.conversation = CloudConversation()
        harness = ContextHarness(SessionJournal(tmp_path / "context.jsonl"))
        harness.record_user("My appointment is at 9.", source="explicit-control", turn_id="previous")
        harness.record_user("What time was it?", source="explicit-control", turn_id="current")
        bridge.set_context_harness(harness)
        bridge.request_response("Use the shared evidence: appointment at 9.", origin={
            "turnId": "current", "inputChannel": "microphone" if action == "microphone" else "explicit-control",
        })
        if action == "microphone":
            assert len(cloud.created) == 1
            assert bridge._text_conversation is None
        else:
            await settle(lambda: bridge._text_conversation is not None)
            text = bridge._text_conversation
            assert text.segmenter is None
            assert not cloud.created
            await text.provider.started.wait()
            if action == "cancel":
                assert bridge.cancel_response()
            elif action == "close":
                await bridge.close()
            await settle(lambda: not text.tasks)
            await asyncio.sleep(.01)
            assert cloud.cancelled == 0
            messages = text.provider.messages[0]
            assert any("My appointment is at 9." in row["content"] for row in messages)
            assert messages[-1]["content"] == "Use the shared evidence: appointment at 9."
            completed = [event for event in events if event.get("type") == "response_status" and event.get("status") == "completed"]
            if action in {"complete", "tts-down"}:
                assert bridge._dialogue_exchanges == 0
                assert harness.journal.entries(types={"provider_usage"})[-1]["contextScope"] == "request"
                assert completed and completed[0]["origin"]["turnId"] == "current"
                assert any(event["type"] == "audio" for event in events) is (action == "complete")
                if action == "tts-down":
                    assert any(event["type"] == "audio_unavailable" for event in events)
                    assert not any(event["type"] == "provider_unavailable" for event in events)
                assert any(event["type"] == "assistant_transcript" and event["done"] for event in events)
                assert any(row.get("message", {}).get("content") == "First. Second." for row in harness.journal.entries(types={"message"}))
            else:
                assert not completed
        await bridge.close()

    asyncio.run(scenario())


def test_text_fallback_is_persisted_without_claiming_audio(tmp_path):
    journal = SessionJournal(tmp_path / "fallback.jsonl")
    ledger = CaseWorkLedger(journal)
    ledger.capture_turn("question", source="explicit-control", turn_id="turn", sequence=1)
    manager = AnswerJobs("test")
    manager.register("user", journal, turn_id="turn", question="question", instructions="context")
    manager.foreground_finished(journal, "turn", "response", audio_available=False)
    journal.append_message("assistant", "Available text", source="provider-output", response_id="response")
    answer = ledger.snapshot()["turnInbox"][0]["answer"]
    assert answer["status"] == "completed"
    assert answer["channel"] == "generated-text"
    assert answer["text"] == "Available text"


@pytest.mark.parametrize('channel', ['microphone', 'explicit-control'])
def test_prepared_visual_uses_delivery_bridge_for_both_channels(tmp_path, monkeypatch, channel):
    monkeypatch.setattr('egoassist.realtime.modular.ModularConversation',
                        partial(ModularConversation, provider_factory=FakeProvider))

    async def run():
        events = []
        async def emit(payload):
            events.append(payload)
        bridge = OmniBrowserBridge('test', 'test', 'Ethan', 'instructions', emit, lambda _: None)
        bridge.connected = True
        cloud = bridge.conversation = CloudConversation()
        harness = ContextHarness(SessionJournal(tmp_path / 'context.jsonl'))
        bridge.set_context_harness(harness)
        bridge.request_response('grounding', origin={'turnId': 'visual', 'inputChannel': channel,
            'preparedAnswer': {'text': '红白格桌布。', 'model': 'actual-vl'}})
        await settle(lambda: any(e.get('type') == 'response_status' and e.get('status') == 'completed' for e in events))
        assert not cloud.created and bridge._text_conversation.provider.messages == []
        assert any(e['type'] == 'audio' for e in events)
        assert any(r.get('message', {}).get('content') == '红白格桌布。' for r in harness.journal.entries(types={'message'}))
        await bridge.close()
    asyncio.run(run())


@pytest.mark.parametrize("original_source,resume_source,expected", [
    ("explicit-control", "explicit-control", "explicit-control"),
    ("explicit-control", "input-audio-transcript", "explicit-control"),
    ("input-audio-transcript", "explicit-control", "explicit-control"),
    ("input-audio-transcript", "input-audio-transcript", "microphone"),
])
def test_resume_without_audio_cache_preserves_text_channel(tmp_path, original_source, resume_source, expected):
    journal = SessionJournal(tmp_path / "resume.jsonl")
    ledger = CaseWorkLedger(journal)
    ledger.capture_turn("Explain multiplication", source=original_source, turn_id="original", sequence=1)
    ledger.capture_turn("Read that answer aloud", source=resume_source, turn_id="resume", sequence=2)
    manager = AnswerJobs("test")
    manager.register("user", journal, turn_id="original", question="Explain multiplication", instructions="evidence")
    manager.foreground_finished(journal, "original", "response", audio_available=False)
    request = Mock()
    asyncio.run(apply_interaction(
        {"interaction": {"audience": "assistant", "action": "resume", "output": "speech", "targetTurnId": "original"}},
        "resume", 2, owner=SimpleNamespace(latest_input_sequence=2, output_generation=2, speech_floor_sequence=1),
        interaction_state=InteractionState(journal), harness=ContextHarness(journal),
        answer_executor=Mock(), user_id="user", bridge=Mock(), ws=SimpleNamespace(send_json=AsyncMock()),
        emit=AsyncMock(), muted_response_ids=set(), resumed_response_ids={}, request_bridge_response=request,
    ))
    request.assert_called_once()
    assert request.call_args.kwargs["origin"]["inputChannel"] == expected
    assert request.call_args.kwargs["origin"]["answerObligation"] == "original"
