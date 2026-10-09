"""Cross-lane contracts, stale evidence fences and bounded worker lifetime."""
import ast
import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from egoassist.core.journal import EvidenceUnavailable, SessionJournal
from egoassist.core.retention import erase_expired
from egoassist.memory.evidence_service import EvidenceService
from egoassist.memory.policy import default_memory_policy
from egoassist.memory.service import MemoryQuery, MemoryService
from egoassist.memory.temporal import ClockContext
from egoassist.memory.timeline import TimelineMemory


def memory_fixture(tmp_path):
    evidence = EvidenceService(tmp_path / 'users')
    journal = SessionJournal(tmp_path / 'users/me/sessions/case.jsonl', session_id='case')
    evidence.ingest('me', journal, [{'id': 'one', 'text': 'The bicycle is blue.',
        'speaker': 'Alice', 'source_at': '2026-09-23T10:00:00+08:00'}])
    source = journal.entries(types={'source.event'})[0]
    timeline = TimelineMemory(tmp_path / 'timeline.sqlite3')
    card = timeline.remember('The bicycle is blue.', user_id='me', case_id='case',
        scope='case', kind='fact', source_ids=[source['id']])
    memory = MemoryService(timeline, evidence, None, read_timeout=0.02)
    query = MemoryQuery('me', 'case', 'bicycle', default_memory_policy(), ClockContext.current())
    return memory, query, journal, card


@pytest.mark.parametrize('change', ['user_id', 'session_id', 'policy', 'clock', 'semantic', 'ranking_query', 'exclude'])
def test_prepared_read_rejects_changed_contract(tmp_path, change):
    memory, query, journal, _ = memory_fixture(tmp_path)
    prepared = memory.read(query)
    policy = deepcopy(query.policy)
    policy['components']['timeline']['episodic_enabled'] = False
    changes = dict(user_id='another', session_id='another', policy=policy,
        clock=ClockContext.current('Asia/Shanghai'), semantic={'memoryQuery': 'referential'},
        ranking_query='another', exclude=('case:another',))
    result = memory.accept(prepared, replace(query, **{change: changes[change]}), journal)
    assert result.retrieval is None and result.evidence is None
    assert result.failures == {'read': 'scope-or-policy-changed'}


def test_prepared_read_drops_deleted_cards_and_sources(tmp_path):
    memory, query, journal, card = memory_fixture(tmp_path)
    prepared = memory.read(query)
    assert prepared.retrieval['selected'] and prepared.evidence['sources']
    memory.timeline.delete(card['id'])
    erase_expired(journal, {}, source_ids=(prepared.evidence['sources'][0]['journalEventId'],))
    accepted = memory.accept(prepared, query, journal)
    assert accepted.retrieval['selected'] == [] and accepted.evidence is None
    assert 'The bicycle is blue.' not in str(accepted.retrieval)
    assert set(accepted.failures) == {'timeline', 'sources'}


def test_prepared_read_drops_superseded_card(tmp_path):
    memory, query, journal, card = memory_fixture(tmp_path)
    prepared = memory.read(query)
    memory.timeline.remember('The bicycle is green.', user_id='me', case_id='case',
        scope='case', kind='fact', replaces_card_id=card['id'])
    accepted = memory.accept(prepared, query, journal)
    assert accepted.retrieval['selected'] == []
    assert accepted.evidence is not None  # Original source history remains valid.


def test_card_is_not_reinjected_after_its_source_is_erased(tmp_path):
    memory, query, journal, card = memory_fixture(tmp_path)
    prepared = memory.read(query)
    erase_expired(journal, {}, source_ids=card['sourceIds'])
    accepted = memory.accept(prepared, query, journal)
    assert not accepted.retrieval['selected'] and accepted.evidence is None


@pytest.mark.parametrize('failed', ['timeline', 'sources'])
def test_one_memory_reader_failure_preserves_other_evidence(tmp_path, monkeypatch, failed):
    memory, query, journal, _ = memory_fixture(tmp_path)
    def broken(*args, **kwargs):
        raise OSError('injected read failure')
    if failed == 'timeline':
        monkeypatch.setattr(memory, '_retrieve', broken)
    else:
        monkeypatch.setattr(memory.evidence, 'query', broken)
    accepted = memory.accept(memory.read(query), query, journal)
    assert accepted.failures == {failed: 'OSError'}
    assert accepted.evidence if failed == 'timeline' else accepted.retrieval['selected']


@pytest.mark.parametrize('cancel_maintenance', [False, True])
def test_timeout_retains_worker_and_maintenance_drains_before_deletion(tmp_path, cancel_maintenance):
    async def run():
        service = EvidenceService(tmp_path / 'users')
        started, release = Event(), Event()
        path = tmp_path / 'late-index'
        def reader():
            started.set()
            assert release.wait(5)
            path.write_text('finished', encoding='utf-8')
        try:
            with pytest.raises(TimeoutError):
                await service.run_read('me', reader, timeout=0.02)
            assert started.is_set() and len(service.readers) == 1
            with pytest.raises(EvidenceUnavailable):
                await service.run_read('me', lambda: None, timeout=1)
            maintenance = asyncio.create_task(service.pause_user('me'))
            await asyncio.sleep(0)
            assert 'me' in service.paused and not maintenance.done()
            if cancel_maintenance:
                maintenance.cancel()
                await asyncio.sleep(0)
                assert not maintenance.done()
            release.set()
            if cancel_maintenance:
                with pytest.raises(asyncio.CancelledError):
                    await maintenance
            else:
                await maintenance
            assert path.exists() and not service.readers
            path.unlink()
            service.resume_user('me')
            assert not service.paused
            await service.close()
            assert not path.exists()
        finally:
            release.set()
            await service.close()
    asyncio.run(run())


@pytest.mark.parametrize('invalidation', ['epoch', 'cancel', 'revision', 'source'])
def test_turn_reads_outside_case_lock_and_rejects_stale_commit(tmp_path, invalidation):
    from egoassist.realtime.turn_coordinator import TurnCoordinator
    from egoassist.realtime.work_ledger import CaseWorkLedger
    async def run():
        memory, _, journal, _ = memory_fixture(tmp_path)
        memory.read_timeout = 2
        ledger = CaseWorkLedger(journal)
        ledger.capture_turn('bicycle', source='test', turn_id='turn')
        coordinator = TurnCoordinator.__new__(TurnCoordinator)
        token = object()
        owner = SimpleNamespace(work_ledger=ledger, applied_controls={}, prepared_scenes={},
            mutation_lock=asyncio.Lock(), socket_token=token, epoch=1, output_generation=1)
        async def scene(*args, **kwargs):
            return None
        owner.scene = SimpleNamespace(context=scene)
        coordinator.owner, coordinator.socket_token, coordinator.connection_epoch = owner, token, 1
        coordinator.user_id, coordinator.session_id = 'me', 'case'
        coordinator.harness = SimpleNamespace(journal=journal)
        coordinator.current_memory_policy = default_memory_policy
        coordinator.clock_for_session = lambda *args: ClockContext.current()
        async def noop(*args):
            pass
        coordinator.turn_progress = noop
        entered, release = Event(), Event()
        original_read = memory.read
        def slow_read(query):
            entered.set()
            assert release.wait(5)
            return original_read(query)
        memory.read = slow_read
        coordinator.memory_service = memory
        semantic = asyncio.create_task(asyncio.sleep(0, result={}))
        task = asyncio.create_task(coordinator.process_queued_turn('bicycle', explicit=False,
            turn_sequence=1, turn_id='turn', semantic_task=semantic))
        try:
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            # A second lane can acquire the real owner lock during the worker read.
            await asyncio.wait_for(owner.mutation_lock.acquire(), timeout=0.2)
            try:
                if invalidation == 'epoch':
                    owner.epoch += 1
                elif invalidation == 'cancel':
                    ledger.set_turn_status('turn', 'cancelled')
                elif invalidation == 'revision':
                    journal.append('turn_revision', {'turnId': 'new', 'supersedesTurnId': 'turn'})
                else:
                    erase_expired(journal, {}, source_ids=(ledger.turn_message('turn')['id'],))
            finally:
                owner.mutation_lock.release()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not any(row.get('status') == 'processing' for row in journal.entries(types={'turn_inbox_state'}))
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await memory.evidence.close()
    asyncio.run(run())


def test_slow_observation_delivery_does_not_hold_commit_lock():
    from egoassist.application.observation_transport import deliver_observation
    async def run():
        lock = asyncio.Lock()
        sending, release = asyncio.Event(), asyncio.Event()
        async def observe(*args):
            async with lock:
                return {'accepted': True, 'ambient': {'count': 1}}
        async def send(payload):
            assert not lock.locked()
            sending.set()
            await release.wait()
        delivery = asyncio.create_task(deliver_observation(SimpleNamespace(session_perception_result=observe),
            {}, {}, ws=SimpleNamespace(closed=False, send_json=send), state=lambda: {}))
        await asyncio.wait_for(sending.wait(), timeout=1)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.2)
            lock.release()
        finally:
            release.set()
        assert (await delivery)['accepted']
    asyncio.run(run())


def test_observation_memory_failure_preserves_source_and_suppresses_clarification(tmp_path):
    from egoassist.realtime.observation_interaction import ObservationInteraction
    from egoassist.core.lane_contracts import AcceptedObservation
    journal = SessionJournal(tmp_path / 'case.jsonl', session_id='case')
    entry = journal.append('ambient_observation', {'status': 'accepted', 'observation_id': 'observation'})
    clock = ClockContext.current()
    captured = SimpleNamespace(observation_mode='open', previous_observation=None,
        frame_captured_at=clock.now_utc, frame_id='frame', media=None)
    hypothesis = SimpleNamespace(visible_evidence=['blue bicycle'], uncertainties=[], notification=None)
    evidence = SimpleNamespace(source='camera/test', received_at=clock.now_utc,
        observation_id='observation', task_id=None, frame_age_ms=0)
    def unavailable(*args):
        raise OSError('injected memory failure')
    consumer = ObservationInteraction.__new__(ObservationInteraction)
    consumer.user_id, consumer.session_id, consumer.journal = 'me', 'case', journal
    consumer.clock_for_session = lambda *args: clock
    consumer.session_runtime = SimpleNamespace(skill=None)
    consumer.current_memory_policy = default_memory_policy
    consumer.memory_service = SimpleNamespace(remember_observation=unavailable, pending_draft=unavailable)
    consumer.owner = SimpleNamespace(
        scene=SimpleNamespace(events=SimpleNamespace(schedule=lambda: None)),
        lanes=SimpleNamespace(reviews=SimpleNamespace(rows={})),
    )
    result = consumer.ambient(AcceptedObservation(captured, hypothesis, evidence, entry, 'accepted'))
    assert result['accepted'] and result['speech'] is None
    assert journal.entries_by_id([entry['id']])[entry['id']] == entry
    assert journal.entries(types={'memory.write.failed'})[0]['error'] == 'OSError'
    assert journal.entries(types={'memory.read'})[0]['status'] == 'degraded'


def test_history_delete_waits_for_worker_before_invalidating_index(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from egoassist.application.history_api import mount
    async def run():
        memory, _, journal, _ = memory_fixture(tmp_path)
        service = memory.evidence
        key = web.RequestKey('test.user', dict)
        @web.middleware
        async def auth(request, handler):
            request[key] = {'id': 'me'}
            return await handler(request)
        app = web.Application(middlewares=[auth])
        mount(app, identity=SimpleNamespace(get_context_session=lambda *args: {}), user_key=key,
            journal_for=lambda *args: journal, evidence=service, answers=None)
        release = Event()
        index = service.index('me')
        def late_sync():
            assert release.wait(5)
            index.sync()
        async with TestClient(TestServer(app)) as client:
            try:
                with pytest.raises(TimeoutError):
                    await service.run_read('me', late_sync, timeout=0.02)
                async def delete():
                    return await client.post('/api/history/delete', json={'conversation_id': 'case', 'source_id': 'one'})
                deletion = asyncio.create_task(delete())
                for _ in range(200):
                    if 'me' in service.paused:
                        break
                    await asyncio.sleep(0.005)
                assert 'me' in service.paused and not deletion.done()
                assert journal.entries(types={'source.event'})
                release.set()
                response = await deletion
                assert response.status == 200, await response.text()
                assert not index.path.exists() and not service.readers and not service.paused
                assert service.index('me').events('case') == []
            finally:
                release.set()
                await service.close()
    asyncio.run(run())


def test_research_turn_rejects_input_erased_during_preparation(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from egoassist import BUILTIN_SKILLS_ROOT
    from egoassist.application.composition import create_app
    original = MemoryService.prepare
    async def prepare_then_erase(self, query):
        prepared = await original(self, query)
        if query.exclude:
            journal = SessionJournal(self.evidence.root / query.user_id / 'sessions' / (query.session_id + '.jsonl'))
            erase_expired(journal, {}, source_ids=(query.exclude[0].split(':', 1)[1],))
        return prepared
    monkeypatch.setattr(MemoryService, 'prepare', prepare_then_erase)
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path / 'runtime.json'), 'unused', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            await client.post('/api/auth/register', json={'username': 'lane-test', 'password': 'lane-password-123'})
            session = (await (await client.post('/api/sessions')).json())['session']['id']
            response = await client.post('/api/voice', json={'sessionId': session, 'transcript': 'The bicycle is blue.',
                'memoryKind': 'fact', 'memoryText': 'The bicycle is blue.', 'memoryScope': 'case'})
            assert response.status == 409, await response.text()
    asyncio.run(run())


def test_long_event_statuses_and_account_diary_remain_separate(tmp_path):
    from egoassist.diary.service import DiaryService
    from egoassist.diary.sources import period_for
    from egoassist.runtime import ThreeLaneRuntime
    journal = SessionJournal(tmp_path / 'me/sessions/case.jsonl', session_id='case')
    diary = DiaryService(tmp_path, tmp_path / 'timeline.sqlite3', '')
    assert diary.status('absent')['jobs'] == {} and not (tmp_path / 'absent').exists()
    row = diary.store('me').create(period_for('daily', '2026-09-22', 'UTC'))
    journal.append('long_event.job', {'requestId': 'old', 'status': 'queued'})
    journal.append('long_event.request', {})
    journal.append('long_event.job', {'requestId': 'new', 'status': 'running'})
    runtime = ThreeLaneRuntime(journal, 'me')
    state = runtime.snapshot(observation=None, scheduler={}, account_diary=diary.status('me'))['memory']
    assert state['jobs']['queued'] == 0 and state['jobs']['running'] == 1
    assert state['accountDiary']['scope'] == 'account'
    assert state['accountDiary']['jobs']['queued'] == 1
    assert state['accountDiary']['activeJobs'][0]['id'] == row['id']
    journal.append('long_event.job', {'requestId': 'new', 'status': 'completed'})
    assert runtime.snapshot(observation=None, scheduler={})['memory']['jobs']['completed'] == 1
    journal.append('long_event.reset', {})
    assert not any(runtime.snapshot(observation=None, scheduler={})['memory']['jobs'].values())


def test_memory_dependency_boundary_and_observation_decision_ownership():
    root = Path(__file__).parents[1] / 'egoassist'
    for path in (root / 'memory').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text('utf-8'))):
            imports = [node.module or ''] if isinstance(node, ast.ImportFrom) else (
                [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            assert not any(name.startswith(('egoassist.realtime', 'egoassist.application')) for name in imports), path
    tree = ast.parse((root / 'realtime/observation_service.py').read_text('utf-8'))
    calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert not {'remember', 'remember_observation', 'consider', 'send_json', 'apply_voice_intent'} & set(calls)
