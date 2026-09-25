"""Long-session regressions: durable channels, visible failures, bounded context."""
import asyncio
from types import SimpleNamespace
import pytest

from egoassist.core.journal import SessionJournal
from egoassist.realtime.context_harness import ContextHarness, ContextSettings, ContextLayer, estimate_tokens
from egoassist.realtime.turn_coordinator import TurnCoordinator
from egoassist.realtime.work_ledger import CaseWorkLedger


@pytest.mark.parametrize('case', ['recover', 'exhausted', 'full-timeout', 'account-blocked'])
def test_router_retries_only_inside_original_budget(tmp_path, case):
    async def run():
        coordinator = object.__new__(TurnCoordinator)
        coordinator.harness = SimpleNamespace(journal=SessionJournal(tmp_path / 'session.jsonl'))
        coordinator.semantic_router = SimpleNamespace(timeout_seconds=.12)
        coordinator.ws = SimpleNamespace(closed=True)
        calls, cancelled = [], []
        async def classify(*args):
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(.13 if case == 'full-timeout' else .01)
                return {'status':'unavailable', 'timedOut':True, 'retryable':case != 'account-blocked'}
            if case == 'exhausted':
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(True)
            return {'status':'classified'}
        coordinator.classify_semantic_turn = classify
        async with asyncio.timeout(.5):
            result = await coordinator.classify_with_retry_budget('hello', 'turn')
        assert len(calls) == (2 if case in {'recover','exhausted'} else 1)
        assert result['status'] == ('classified' if case == 'recover' else 'unavailable')
        assert bool(cancelled) == (case == 'exhausted')
        assert bool(result.get('retryBudgetExhausted')) == (case == 'exhausted')
    asyncio.run(run())


def test_recovered_text_turn_keeps_its_channel(tmp_path):
    async def run():
        journal = SessionJournal(tmp_path / 'session.jsonl')
        ledger = CaseWorkLedger(journal)
        captured = ledger.capture_turn('hello', source='explicit-control', sequence=1)
        token = object()
        coordinator = object.__new__(TurnCoordinator)
        owner = SimpleNamespace(turn_queue=asyncio.Queue(4), turn_worker=asyncio.current_task(),
            socket_token=token, epoch=1, queued_turn_ids=set(), classification_tasks=set(),
            turn_sequence=0, latest_input_sequence=0, pending_semantic_sequences=set())
        coordinator.owner, coordinator.socket_token, coordinator.connection_epoch = owner, token, 1
        async def classify(*args): return {'status': 'classified'}
        coordinator.classify_persisted_turn = classify
        assert coordinator.enqueue_persisted_turn(captured)  # Recovery has no explicit kwarg.
        item = owner.turn_queue.get_nowait()
        assert item['explicit'] is True
        await item['semanticTask']
    asyncio.run(run())


def test_failed_router_is_visible_and_never_completed_as_silent_control(tmp_path):
    async def run():
        journal = SessionJournal(tmp_path / 'session.jsonl')
        ledger = CaseWorkLedger(journal)
        ledger.capture_turn('hello', source='explicit-control', sequence=1, turn_id='text')
        coordinator = object.__new__(TurnCoordinator)
        coordinator.owner = SimpleNamespace(work_ledger=ledger, prepared_scenes={}, applied_controls={})
        coordinator.harness = SimpleNamespace(journal=journal)
        sent, resolved = [], []
        async def send(value): sent.append(value)
        coordinator.ws = SimpleNamespace(closed=False, send_json=send)
        coordinator.resolve_semantic_turn = lambda *args: resolved.append(args)
        async def classify(*args):
            return {'status':'unavailable', 'timedOut':True,
                    'interaction':{'action':'uncertain','audience':'uncertain','output':'silent'}}
        coordinator.classify_persisted_turn = classify
        result = await coordinator.process_queued_turn('hello', explicit=False, turn_sequence=1, turn_id='text')
        assert result['status'] == 'failed'
        assert ledger.turns()['text']['status'] == 'failed'
        assert any(r['type'] == 'error' and r['turnId'] == 'text' for r in sent)
        assert not any(r.get('type') == 'speech_control' for r in sent)
        assert len(resolved) == 1
    asyncio.run(run())


def test_replay_honors_build_budget_even_for_one_oversized_message(tmp_path):
    journal = SessionJournal(tmp_path / 'session.jsonl')
    harness = ContextHarness(journal, ContextSettings(context_window=2048, reserve_tokens=512,
        keep_recent_tokens=256, max_summary_tokens=128))
    harness.record_user('x' * 24000, source='explicit-control')
    layers = [ContextLayer('system', 'Required policy', required=True)]
    harness.build(layers)
    replay = harness.provider_replay_items()
    assert sum(estimate_tokens(item['content'][0]['text']) for item in replay) < harness.settings.input_budget
    assert 'x' * 24000 in str(journal.entries(types={'message'}))  # Original is not erased.


def test_filtered_cursor_advances_over_unrelated_media_and_resets_after_rewrite(tmp_path):
    journal = SessionJournal(tmp_path / 'session.jsonl')
    journal.append('media', {'audio': 'large'})
    turn = journal.append('turn_inbox', {'turnId': 'one'})
    cursor, rows, reset = journal.read_since(types={'turn_inbox'})
    assert reset and [r['id'] for r in rows] == [turn['id']]
    journal.append('media', {'audio': 'more'})
    cursor, rows, reset = journal.read_since(cursor, types={'turn_inbox'})
    assert not rows and not reset
    journal.rewrite(lambda rows: [r for r in rows if r['type'] != 'turn_inbox'])
    _, rows, reset = journal.read_since(cursor, types={'turn_inbox'})
    assert reset and not rows


def test_per_turn_context_is_not_appended_to_persistent_model_history():
    from egoassist.realtime.omni import OmniBrowserBridge
    bridge = object.__new__(OmniBrowserBridge)
    traces = []
    bridge._trace = lambda *args, **kwargs: traces.append(args)
    # No live conversation exists: per-response data must not send a create_item.
    assert bridge.inject_context_data('current_turn_context', {'transcript': 'hello'})
    assert traces[-1][0] == 'context.data.request_scoped'


def test_answer_poll_reads_history_constant_times(tmp_path, monkeypatch):
    import egoassist.product.answer_jobs as module
    from datetime import datetime, timezone
    async def run():
        journal = SessionJournal(tmp_path / 'session.jsonl')
        for i in range(90):
            journal.append('answer.job', {'turnId':str(i), 'status':'completed', 'context':{'text':'x'*2000}})
        journal.append('answer.job', {'turnId':'active','status':'foreground',
            'createdAt':datetime.now(timezone.utc).isoformat()})
        service = module.AnswerJobs('unused')
        service.journals[str(journal.path)] = ('me',journal)
        original, calls = module.answer_jobs, []
        def counted(j):
            calls.append(1)
            return original(j)
        monkeypatch.setattr(module, 'answer_jobs', counted)
        task = asyncio.create_task(service.run())
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
        assert len(calls) == 2  # Independent of the number of historical answers.
    asyncio.run(run())


def test_websocket_memory_prefetch_survives_execution_enrichment(tmp_path, monkeypatch):
    import test_realtime_api as helper
    from aiohttp.test_utils import TestClient, TestServer
    monkeypatch.setenv('EGOASSIST_TURN_ROUTER_BACKEND', 'qwen')
    bridges = helper.install_queue_test_bridge(monkeypatch)
    async def classify(self, text, catalog): return helper.semantic_result(text)
    monkeypatch.setattr(helper.run_egoassist.SemanticTurnRouter, 'classify', classify)
    async def run():
        app = helper.create_app(str(helper.BUILTIN_SKILLS_ROOT),None,str(tmp_path/'memory.json'),'test-realtime','Ethan')
        async with TestClient(TestServer(app)) as client:
            user = await helper.register_client(client,'prefetch-contract')
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            ws = await client.ws_connect('/ws/realtime?session='+sid)
            await ws.receive_json(); await ws.receive_json()
            await ws.send_json({'type':'control','clientRequestId':'hello','transcript':'hello'})
            async with asyncio.timeout(5):
                while True:
                    event = await ws.receive_json()
                    if event.get('type') == 'interaction_status' and event.get('status') == 'generating': break
            journal = SessionJournal(tmp_path/'users'/user['id']/'sessions'/f'{sid}.jsonl')
            reads = journal.entries(types={'memory.read'})
            assert reads and all('scope-or-policy-changed' not in str(r) for r in reads)
            await asyncio.wait_for(bridges[0].request_event.wait(), 2)
            instructions = bridges[0].requests[-1][0]
            assert '<current_turn_context trust="untrusted-data">' in instructions
            assert '"retrievalEvidence"' in instructions and '"clock"' in instructions
            await ws.close()
    asyncio.run(run())


def test_foreground_event_projection_marks_omissions_and_preserves_source_text():
    from copy import deepcopy
    from egoassist.realtime.interaction_window import answer_scene_context
    events = [{'sourceIds':['a','b','c'], 'sources':[
        {'sourceId':sid,'text':'original '+sid,'at':'2026-09-24T00:00:00Z','media':{'asset':'image'}}
        for sid in ['a','b','c']], 'phases':[{'sourceIds':['a']} for _ in range(7)], 'totalPhases':7}
        for _ in range(6)]
    original = {'longEvents':{'events':events,'totalEvents':6,'truncated':False}}
    before = deepcopy(original)
    projected = answer_scene_context(original)['longEvents']
    assert original == before
    assert projected['truncated'] and len(projected['events']) == 2
    first = projected['events'][0]
    assert first['sourcesTruncated'] and first['phasesTruncated']
    assert first['sourceIds'] == ['b','c']
    assert [s['text'] for s in first['sources']] == ['original b','original c']


def test_compact_replay_preserves_roles_sources_and_rewrite_invalidation(tmp_path):
    from egoassist.memory.grounding import HISTORY_STATEMENT_POLICY
    journal = SessionJournal(tmp_path / 'case.jsonl')
    harness = ContextHarness(journal)
    first = journal.append_message('user', 'Yesterday I did not take it.', source='test',
        metadata={'sourceRecordedAt': '2026-09-20T20:00:00+08:00'})
    journal.append_message('assistant', 'Which item?', source='test')
    original = harness.provider_replay_items()
    compact = harness.provider_replay_items(compact_timestamps=True)
    for old, new in zip(original, compact):
        assert old['role'] == new['role']
        assert old['content'][0]['text'].split('\n', 1)[1] == new['content'][0]['text'].split('\n', 1)[1]
    assert '2026-09-20T20:00:00+08:00' in compact[0]['content'][0]['text']
    assert 'relative dates and pronouns' in HISTORY_STATEMENT_POLICY.lower()
    assert 'keep it unknown' in HISTORY_STATEMENT_POLICY
    journal.rewrite(lambda rows: [row for row in rows if row['id'] != first['id']])
    assert 'Yesterday' not in str(harness.provider_replay_items(compact_timestamps=True))


def test_evidence_wire_projection_preserves_meaning_and_original_packet():
    from copy import deepcopy
    from egoassist.product.answer_service import answer_context, provider_evidence_context
    source = {'sourceId': 'case:event', 'journalEventId': 'event', 'externalId': 'import-id',
        'text': 'Yesterday I did not finish.', 'sourceAt': '2026-09-21T20:00:00+08:00',
        'receivedAt': '2026-09-22T20:00:00+08:00', 'timezone': 'Asia/Shanghai',
        'speaker': 'unknown', 'kind': 'user-statement', 'sourceHash': 'hash',
        'span': [0, 27], 'fullTextLength': 100, 'supersedes': 'case:old',
        'eventTime': None, 'validTime': None, 'uncertainties': ['quoted claim'],
        'mediaRefs': [{'assetId': 'image'}], 'extractionStatus': 'not-required',
        'vectorStatus': 'not-indexed'}
    packet = {'sources': [source], 'conversation': [source], 'status': 'retrieved',
        'coverage': {'candidateTruncated': True}, 'snapshotId': 'snapshot'}
    before = deepcopy(packet)
    full, wire = answer_context(packet), provider_evidence_context(packet)
    assert packet == before
    omitted = {'journalEventId', 'sourceHash', 'extractionStatus',
        'vectorStatus', 'timeAnnotationVersion', 'timeExpressionBoundary'}
    for key in ('sources', 'conversation'):
        assert len(full[key]) == len(wire[key])
        assert wire[key][0] == {k: v for k, v in full[key][0].items() if k not in omitted}
        assert full[key][0]['timeExpressionBoundary'] in wire['timeExpressionBoundaries']
    assert len(wire['timeExpressionBoundaries']) == 1
    assert wire['coverage'] == full['coverage']


def test_display_limits_do_not_change_execution_ledgers(tmp_path):
    journal = SessionJournal(tmp_path / 'case.jsonl')
    ledger = CaseWorkLedger(journal)
    for i in range(4):
        turn = ledger.capture_turn(f'question {i}', source='test', turn_id=str(i))
        intent = ledger.create_intent(turn['turnId'], kind='background-work', status='running-background')
        job = ledger.create_job(intent['intentId'], kind='timer', status='running')
        ledger.enqueue_result(intent['intentId'], f'result {i}', job_id=job['jobId'])
    full = ledger.snapshot()
    bounded = ledger.snapshot(limit=2, work_limit=2)
    for key in ('turnInbox', 'intents', 'jobs', 'resultInbox'):
        assert bounded[key] == full[key][-2:]
    bounded['intents'][0]['detail'] = {'modified': True}
    assert len(ledger.intents()) == len(ledger.jobs()) == len(ledger.pending_results()) == 4
    assert ledger.snapshot() == full


def test_lane_projection_counts_and_updates_without_copying_job_payloads(tmp_path, monkeypatch):
    from egoassist.runtime import ThreeLaneRuntime
    import egoassist.core.journal as journal_module
    journal = SessionJournal(tmp_path / 'case.jsonl')
    lane = ThreeLaneRuntime(journal, 'test-user')
    journal.append('answer.job', {'turnId': 'a', 'status': 'foreground',
        'context': {'privatePrompt': 'large history'}})
    journal.append('memory.recovery.job', {'turnId': 'm', 'status': 'running',
        'phase': 'reading', 'scene': {'privatePrompt': 'large history'}})
    original = journal_module.deepcopy
    def guarded(value):
        assert 'privatePrompt' not in str(value)
        return original(value)
    monkeypatch.setattr(journal_module, 'deepcopy', guarded)
    result = lane.snapshot(observation=None, scheduler={})
    assert result['interaction']['answers']['foreground'] == 1
    assert result['memory']['jobs']['running'] == 1
    assert result['memory']['activeJobs'][0]['id'] == 'm'
    journal.append('answer.job', {'turnId': 'a', 'status': 'completed'})
    journal.append('memory.recovery.job', {'turnId': 'm', 'status': 'cancelled'})
    updated = lane.snapshot(observation=None, scheduler={})
    assert updated['interaction']['answers']['completed'] == 1
    assert updated['interaction']['answers']['foreground'] == 0
    assert updated['memory']['activeJobs'] == []
