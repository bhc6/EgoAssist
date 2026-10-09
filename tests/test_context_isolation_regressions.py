"""Regression cases from the September 30 context audit."""
import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.memory.temporal import ClockContext
from egoassist.realtime.router_context import conversation_at_turn
from egoassist.realtime.turn_coordinator import TurnCoordinator
from egoassist.realtime.work_ledger import CaseWorkLedger
from egoassist.realtime.omni import OmniBrowserBridge, _ResponseRequest
from run_egoassist import create_app
from tests.test_modular_realtime import conversation, settle
from tests.test_realtime_api import install_queue_test_bridge, patch_semantic_router, register_client


def test_delayed_turn_uses_journal_order_even_with_equal_times_and_reset_sequences(tmp_path):
    journal = SessionJournal(tmp_path / 'session.jsonl')
    ledger = CaseWorkLedger(journal)
    ledger.capture_turn('old session', source='test', sequence=2, turn_id='old')
    ledger.capture_turn('actual previous question', source='test', sequence=1, turn_id='previous')
    journal.append_message('assistant', 'previous answer', source='test')
    ledger.capture_turn('current question', source='test', sequence=2, turn_id='current')
    for index in range(30):
        ledger.capture_turn('future question ' + str(index), source='test', sequence=index+3, turn_id='future-'+str(index))
    journal.rewrite(lambda rows: [{**row, 'timestamp': '2026-09-30T12:00:00+00:00'} for row in rows])
    history = conversation_at_turn(journal, 'current')
    assert history['priorTurnIds'] == ['old', 'previous']
    assert [row['text'] for row in history['messages']] == ['old session', 'actual previous question', 'previous answer']
    assert conversation_at_turn(journal, 'unknown')['messages'] == []
    journal.rewrite(lambda rows: [row for row in rows if row.get('message', {}).get('content') != 'previous answer'])
    assert 'previous answer' not in str(conversation_at_turn(journal, 'current'))


def test_router_previous_turn_and_answer_ignore_legacy_sequence_order(tmp_path):
    async def run():
        journal = SessionJournal(tmp_path / 'session.jsonl')
        ledger = CaseWorkLedger(journal)
        for turn_id, sequence in [('old', 1), ('previous', 1), ('current', 2), ('future', 3)]:
            ledger.capture_turn(turn_id, source='test', sequence=sequence, turn_id=turn_id)
            journal.append('answer.job', {'turnId': turn_id, 'question': turn_id, 'status': 'completed'})
        coordinator = object.__new__(TurnCoordinator)
        coordinator.harness = SimpleNamespace(journal=journal)
        coordinator.owner = SimpleNamespace(work_ledger=ledger, scene=SimpleNamespace(turns={}))
        coordinator.registry = SimpleNamespace(routing_catalog=lambda: [])
        coordinator.user_id, coordinator.session_id = 'user', 'session'
        coordinator.clock_for_session = lambda *_: ClockContext.current()
        coordinator.current_memory_policy = lambda: {'components': {'timeline': {'preference_enabled': True, 'episodic_enabled': True}}}
        coordinator.timeline = SimpleNamespace(cards=lambda **_: [])
        coordinator.session_runtime = SimpleNamespace(task=None)
        coordinator.memory_service = SimpleNamespace(pending_draft=lambda *_: None)
        coordinator.interaction_state = SimpleNamespace(playback_context=lambda: {}, snapshot=lambda: {})
        captured = {}
        async def classify(*_): return {'status': 'classified'}
        coordinator.semantic_router = SimpleNamespace(set_turn_context=lambda **kw: captured.update(kw), classify=classify)
        await coordinator.classify_semantic_turn('current', 'current')
        assert captured['previous_turn']['turnId'] == 'previous'
        assert captured['previous_answer']['turnId'] == 'previous'
        assert [r['turnId'] for r in captured['answers']] == ['old', 'previous']
        assert [r['text'] for r in captured['recent_messages']] == ['old', 'previous']
    asyncio.run(run())


def test_websocket_restores_completed_turn_sequence_after_restart(tmp_path, monkeypatch):
    instances = install_queue_test_bridge(monkeypatch)
    patch_semantic_router(monkeypatch)
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path/'runtime.json'), 'test', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            await register_client(client)
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            async with client.ws_connect('/ws/realtime?session='+sid) as ws:
                await ws.receive_json(); await ws.receive_json()
                journal = instances[-1].harness.journal
                ledger = CaseWorkLedger(journal)
                ledger.capture_turn('finished before restart', source='explicit-control', sequence=91, turn_id='finished')
                ledger.set_turn_status('finished', 'completed')
                assert not ledger.recoverable_turns()
            async with client.ws_connect('/ws/realtime?session='+sid) as ws:
                await ws.receive_json(); await ws.receive_json()
                await ws.send_json({'type': 'control', 'transcript': 'new question', 'clientRequestId': 'new-question'})
                await asyncio.wait_for(instances[-1].request_event.wait(), timeout=3)
                assert ledger.turns()['new-question']['sequence'] == 92
    asyncio.run(run())


@pytest.mark.parametrize('visual', [False, True])
def test_model_input_and_audit_freeze_before_event_loop_yields(visual):
    async def run():
        c, callback = conversation()
        c.update_session(enable_input_audio_transcription=not visual, instructions='original policy')
        c.append_video('original frame')
        c.create_item({'id': 'original', 'type': 'message', 'role': 'user', 'content': 'original question'})
        original = c.provider.observe if visual else c.provider.text_stream
        def audit():
            c.provider.audit({'auditEvent': 'request', 'callId': 'test-call', 'stage': 'vision' if visual else 'text'})
        if visual:
            async def observe(*args):
                audit()
                return await original(*args)
            c.provider.observe = observe
        else:
            async def text_stream(*args):
                audit()
                async for token in original(*args): yield token
            c.provider.text_stream = text_stream
        c.create_response(instructions='answer current question', output_modalities=['text'])
        c.update_session(enable_input_audio_transcription=visual, instructions='future policy')
        c.append_video('future frame')
        c.create_item({'id': 'future', 'type': 'message', 'role': 'user', 'content': 'future question'})
        await settle(lambda: any(e['type'] == 'response.done' for e in callback.events))
        if visual:
            assert c.provider.frames == [('answer current question', 'original frame')]
            assert not c.provider.messages
        else:
            contents = [m['content'] for m in c.provider.messages[0]]
            assert contents == ['original policy', 'original question', 'answer current question']
            assert not c.provider.frames
        audit_row = next(e for e in callback.events if e['type'] == 'pipeline.audit')
        assert audit_row['contextItemIds'] == ['original']
        assert 'future' in c.item_ids  # New input is retained for the next response.
        await c.aclose()
    asyncio.run(run())


def test_text_adapter_keeps_context_captured_before_queued_message(tmp_path, monkeypatch):
    async def run():
        from egoassist.realtime.context_harness import ContextHarness, ContextSettings
        bridge = object.__new__(OmniBrowserBridge)
        import threading
        bridge._response_lock = threading.Lock()
        bridge._response_sequence = 0
        bridge._context_harness = ContextHarness(SessionJournal(tmp_path / 'events.jsonl'), ContextSettings())
        bridge._context_harness.record_user('captured question', source='test')
        bridge._queued_requests = []
        bridge._response_pending = False
        bridge._active_response_ids = set()
        bridge._response_text = {}
        bridge._trace = lambda *args, **kwargs: None
        bridge._send_response = lambda request: None
        request = _ResponseRequest('answer', 50, 1, output_mode='text', context_items=tuple(
            bridge._context_harness.provider_replay_items(compact_timestamps=True)))
        bridge._context_harness.record_user('future question', source='test')
        class FakeConversation:
            def __init__(self): self.items = []; self.item_ids = set(); self.model = 'test'
            def create_item(self, item):
                self.items.append(item)
                if item.get('id'): self.item_ids.add(item['id'])
            def connect(self): pass
            def update_session(self, **_): pass
            def create_response(self, **_): pass
        conversation = FakeConversation()
        monkeypatch.setattr('egoassist.realtime.modular.ModularConversation', lambda **_: conversation)
        bridge.closed = False
        bridge._inflight_response = request
        bridge._cancel_when_created = False
        bridge.instructions = ''
        bridge.model = 'test'
        bridge.api_key = 'test'
        bridge._dialogue_generation = 0
        bridge._Callback = lambda *_: None
        bridge._text_conversation = conversation
        bridge._start_text_response(request)
        assert len(conversation.items) == 1
        assert 'captured question' in str(conversation.items[0])
        assert 'future question' not in str(conversation.items)
    asyncio.run(run())
