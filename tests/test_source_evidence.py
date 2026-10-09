import asyncio
from datetime import datetime, timezone
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.application.composition import create_app
from egoassist.core.journal import SessionJournal
from egoassist.core.retention import erase_expired
from egoassist.core.source_events import history_event
from egoassist.memory.evidence_service import EvidenceService
from egoassist.memory.source_index import SourceIndex
from egoassist.product.answer_service import evidence_instructions, validate_answer


def setup(tmp_path):
    journal = SessionJournal(tmp_path / 'users' / 'me' / 'sessions' / 'case.jsonl', session_id='case')
    service = EvidenceService(tmp_path / 'users')
    return service, journal


def event(identifier, text, **extra):
    return {'id': identifier, 'text': text, 'speaker': 'Alice', 'source_at': '2023-05-08T16:00:00', **extra}


def test_source_ingestion_immutable_idempotent_and_extraction_independent(tmp_path):
    service, journal = setup(tmp_path)
    values = [event('D1:1', 'My bicycle is blue. It is in the garden shed.')]
    result = service.ingest('me', journal, values)
    assert result['status'] == 'indexed' and result['events'][0]['extraction'] == 'not-requested'
    assert service.ingest('me', journal, values) == result
    assert len(journal.entries(types={'source.event'})) == 1
    with pytest.raises(ValueError, match='immutable'):
        service.ingest('me', journal, [event('D1:1', 'new text')])
    packet = service.query('me', 'case', 'What color is the bicycle?')
    assert packet['sources'][0]['text'] == values[0]['text']
    assert packet['sources'][0]['sourceAt'] == '2023-05-08T16:00:00'
    assert packet['sources'][0]['timezone'] is None
    assert packet['sources'][0]['eventTime'] is None
    assert packet['sources'][0]['receivedAt'] != packet['sources'][0]['sourceAt']


def test_long_source_tail_and_chinese_are_searchable_and_index_rebuilds(tmp_path):
    service, journal = setup(tmp_path)
    values = [event('long', 'unrelated context ' * 1000 + 'The telescope is in the observatory.'),
              event('cn', '备用钥匙在书房黄色抽屉。')]
    service.ingest('me', journal, values)
    packet = service.query('me', 'case', 'Where is the telescope?')
    selected = next(row for row in packet['sources'] if 'telescope' in row['text'])
    assert selected['span'][0] > 10000
    assert service.query('me', 'case', '备用钥匙在哪里')['sources'][0]['externalId'] == 'cn'
    original = journal.path.read_bytes()
    service.index('me').path.unlink()
    assert len(service.index('me').events('case')) == 2
    assert journal.path.read_bytes() == original


def test_snapshot_scope_freezes_sources_but_cannot_survive_deletion(tmp_path):
    service, journal = setup(tmp_path)
    service.ingest('me', journal, [event('a', 'The telescope is blue.')])
    packet = service.query('me', 'case', 'telescope')
    service.ingest('me', journal, [event('b', 'The telescope is now green.', supersedes='a')])
    assert service.query('me', 'case', 'telescope', snapshot_id=packet['snapshotId']) == packet
    with pytest.raises(ValueError): service.query('me', 'other', 'telescope', snapshot_id=packet['snapshotId'])
    with pytest.raises(ValueError): service.query('me', 'case', 'another question', snapshot_id=packet['snapshotId'])
    journal.append('evidence.package', {'packet': packet})
    erase_expired(journal, {}, source_ids=(packet['sources'][0]['journalEventId'],))
    assert 'The telescope is blue.' not in journal.path.read_text('utf-8')
    with pytest.raises(ValueError): service.query('me', 'case', 'telescope', snapshot_id=packet['snapshotId'])
    assert all(row['externalId'] != 'a' for row in service.index('me').events('case'))


def test_candidate_audit_complete_and_neighbors_preserve_turn_ids(tmp_path):
    service, journal = setup(tmp_path)
    service.ingest('me', journal, [event(str(i), f'Alice bicycle event {i}.') for i in range(40)])
    packet = service.query('me', 'case', 'bicycle')
    assert len(packet['audit']) == 40 and packet['coverage']['sourceEvents'] == 40
    assert not packet['coverage']['candidateTruncated']
    assert all(row['semanticFilter'] is False for row in packet['audit'])
    assert any(row['retrievalReason'] == 'adjacent-turn' for row in packet['sources'])
    assert len({row['externalId'] for row in service.index('me').events('case')}) == 40


def test_source_times_and_claim_citations_fail_closed(tmp_path):
    assert history_event(event('d', 'yesterday', source_at=None))['source_at'] is None
    assert history_event(event('d', 'yesterday', source_at='2023-05-08'))['source_at'] == '2023-05-08'
    with pytest.raises(ValueError): history_event(event('d', 'text', source_at='yesterday'))
    with pytest.raises(ValueError): history_event({**event('d', 'text'), 'user_id': 'other'})
    service, journal = setup(tmp_path)
    service.ingest('me', journal, [event('a', 'Blue bicycle')])
    packet = service.query('me', 'case', 'bicycle')
    with pytest.raises(ValueError):
        validate_answer({'answer': 'red', 'claims': [{'text': 'red', 'supporting_source_ids': ['invented']}],
            'unanswered_parts': [], 'conflicts': [], 'status': 'supported'}, packet)
    assert packet['snapshotId'] in evidence_instructions(packet)
    assert 'receivedAt is ingestion time' in evidence_instructions(packet)


def test_history_api_long_import_scope_deletion_and_old_protocol(tmp_path):
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path/'runtime.json'), 'unused', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            assert (await client.post('/api/history/import', json={})).status == 401
            await client.post('/api/auth/register', json={'username': 'source-one', 'password': 'source-password-123'})
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            payload = {'conversation_id': sid, 'events': [event('a', 'context ' * 200 + 'blue bicycle')]}
            response = await client.post('/api/history/import', json=payload)
            assert response.status == 201, await response.text()
            packet = await (await client.post('/api/history/query', json={'conversation_id': sid, 'question': 'bicycle'})).json()
            assert packet['coverage']['sourceEvents'] == 1
            assert any(route.resource.canonical == '/ws/realtime' for route in app.router.routes())
            assert (await client.get('/api/insights', params={'session': sid})).status == 200
            await client.post('/api/auth/logout')
            await client.post('/api/auth/register', json={'username': 'source-two', 'password': 'source-password-123'})
            assert (await client.post('/api/history/query', json={'conversation_id': sid, 'question': 'bicycle'})).status in {400, 403}
            await client.post('/api/auth/logout')
            await client.post('/api/auth/login', json={'username': 'source-one', 'password': 'source-password-123'})
            assert (await client.post('/api/history/delete', json={'conversation_id': sid, 'source_id': 'a'})).status == 200
            row = await (await client.post('/api/history/query', json={'conversation_id': sid, 'question': 'bicycle'})).json()
            assert row['coverage']['sourceEvents'] == 0
            assert (await client.post('/api/history/import', json=payload)).status == 400
    asyncio.run(run())


def test_history_http_deletion_preserves_unrelated_snapshot(tmp_path):
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path/'runtime.json'), 'unused', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            await client.post('/api/auth/register', json={'username': 'snapshot-user', 'password': 'snapshot-password-123'})
            snapshots = []
            for identifier in ('parcel', 'bicycle'):
                sid = (await (await client.post('/api/sessions')).json())['session']['id']
                response = await client.post('/api/history/import', json={
                    'conversation_id': sid, 'events': [event(identifier, identifier+' source detail')]})
                assert response.status == 201
                query = {'conversation_id': sid, 'question': identifier}
                response = await client.post('/api/history/query', json=query)
                assert response.status == 200
                packet = await response.json()
                snapshots.append((query, packet))
            assert (await client.post('/api/history/delete', json={
                'conversation_id': snapshots[0][0]['conversation_id'], 'source_id': 'parcel'})).status == 200
            for index, (query, packet) in enumerate(snapshots):
                response = await client.post('/api/history/query', json={**query, 'evidence_snapshot_id': packet['snapshotId']})
                assert response.status == (400 if index == 0 else 200)
                if index:
                    assert await response.json() == packet
            cache = next((tmp_path/'users').glob('*/source-index.sqlite3'))
            assert b'parcel source detail' not in cache.read_bytes()
    asyncio.run(run())


def test_source_calendar_hints_keep_unknown_timezone_and_per_expression_dates():
    from egoassist.memory.source_time import annotate
    source = {'sourceAt': '2023-05-08T17:00:00', 'timezone': None,
        'text': 'I joined yesterday. Last year I painted. Next month I plan a trip.'}
    result = annotate(source)
    expressions = {r['expression'].lower(): r for r in result['timeExpressions']}
    assert expressions['yesterday']['localDate'] == '2023-05-07'
    assert expressions['last year']['calendarPeriod'] == '2022'
    assert expressions['next month']['calendarPeriod'] == '2023-06'
    assert result['timezone'] is None and source.get('timeExpressions') is None
    assert annotate({**source, 'sourceAt': None})['timeExpressions'] == []


def test_live_and_recovered_context_share_snapshot_and_sources(tmp_path):
    from egoassist.product.answer_service import turn_answer_context, answer_context
    from egoassist.product.answer_jobs import AnswerJobs, answer_jobs
    async def run():
        service, journal = setup(tmp_path)
        service.ingest('me', journal, [event('a', 'I joined yesterday.')])
        packet = service.query('me', 'case', 'When did Alice join?')
        context = turn_answer_context(packet['question'], {'evidencePackage': packet},
            clock={'localNow': '2026-09-17'}, runtime={}, shared_scene={})
        delivered = json.loads(json.dumps(context))
        manager = AnswerJobs('unused')
        manager.register('me', journal, turn_id='q1', question=packet['question'],
            instructions=evidence_instructions(packet), context=context)
        restored = answer_jobs(SessionJournal(journal.path))['q1']
        assert restored['context'] == delivered
        assert restored['context']['evidencePackage'] == answer_context(packet)
        captured = []
        async def complete(messages, **kwargs):
            captured.extend(messages)
            return {'output': {'choices': [{'message': {'content': 'May 7, 2023'}, 'finish_reason': 'stop'}]}, 'request_id': 'test'}
        manager.answer_service.complete = complete
        await manager._generate(journal, restored)
        model_context = json.loads(captured[0]['content'].split('\n', 1)[1])
        assert model_context['evidenceAtQuestion']['evidencePackage'] == delivered['evidencePackage']
        await manager.close()
    asyncio.run(run())


def test_legacy_long_transcript_keeps_full_execution_payload():
    from egoassist.realtime.intent_decomposition import normalize_intent_candidates
    text = 'An independent original source. ' * 100 + 'Last turn must survive.'
    candidate = normalize_intent_candidates(text, {})[0]
    assert len(candidate.text) == 500
    assert candidate.execution_text == text
    candidates = normalize_intent_candidates(text, {'intents': [
        {'kind': 'background-job', 'text': text}, {'kind': 'background-job', 'text': text + ' changed'}]})
    assert len(candidates) == 2 and candidates[1].execution_text.endswith(' changed')


def test_diary_uses_original_source_time_and_marks_unknown_dates(tmp_path):
    from egoassist.diary.sources import collect, period_for
    service, journal = setup(tmp_path)
    service.ingest('me', journal, [event('dated', 'Blue bicycle', source_at='2023-05-08T16:00:00+08:00'),
                                  event('unknown', 'A historical event', source_at=None)])
    report = collect(journal.path.parent.parent, tmp_path/'missing.db', 'me', period_for('daily', '2023-05-08', 'Asia/Shanghai'))
    assert report['records'][0]['kind'] == 'historical-dialogue'
    assert report['records'][0]['localDate'] == '2023-05-08'
    assert len(report['coverage']['unplacedSources']) == 1
    assert report['coverage']['unplacedSources'][0]['sourceAt'] is None


def test_unavailable_provider_is_distinct_from_missing_evidence_and_retries_are_logged(tmp_path):
    from aiohttp import ClientConnectionError
    from egoassist.product.answer_service import AnswerService
    async def run():
        service, journal = setup(tmp_path)
        service.ingest('me', journal, [event('a', 'Blue bicycle')])
        packet = service.query('me', 'case', 'bicycle')
        answerer = AnswerService('test')
        async def unavailable(*args, **kwargs):
            raise ClientConnectionError('test network unavailable')
        answerer.complete = unavailable
        answer = await answerer.answer(journal, 'bicycle', packet)
        assert answer['status'] == 'unavailable'
        assert len(journal.entries(types={'evidence.answer.failure'})) == 2
        assert len(journal.entries(types={'evidence.answer.request'})) == 2
        await answerer.close()
    asyncio.run(run())


def test_legacy_audit_retains_all_candidates_without_expanding_polling_payload(tmp_path):
    from egoassist.memory import TimelineMemory
    from egoassist.realtime.episodes import RealtimeEpisodeMemory
    timeline = TimelineMemory(tmp_path/'timeline.sqlite3')
    for index in range(35):
        timeline.remember('Alice bicycle note ' + str(index), user_id='u', case_id='c',
            scope='case', kind='event', card_id=str(index))
    adapter = RealtimeEpisodeMemory(tmp_path/'episodes.json', timeline=timeline,
        owner_user_id='u', owner_case_id='c', scope='case')
    result = adapter.retrieve('bicycle', session_id='c', user_scope='u')
    assert len(result['candidateAudit']) == 35 and len(result['candidates']) == 20
    adapter.mark_injected(result)
    assert 'candidateAudit' not in adapter._last_retrieval_by_session['c']


def test_erasure_follows_historical_snapshot_and_serialized_job_audits(tmp_path):
    service, journal = setup(tmp_path)
    service.ingest('me', journal, [event('a', 'private-secret-123')])
    packet = service.query('me', 'case', 'private-secret')
    journal.append('evidence.package', {'packet': packet})
    journal.append('evidence.answer.request', {'snapshotId': packet['snapshotId'], 'messages': json.dumps(packet)})
    journal.append('evidence.answer.response', {'snapshotId': packet['snapshotId'], 'rawOutput': 'private-secret-123'})
    journal.append('answer.job', {'turnId': 't', 'context': {'evidencePackage': packet}})
    journal.append('answer.request', {'turnId': 't', 'messages': json.dumps(packet)})
    journal.append('answer.result', {'turnId': 't', 'text': 'private-secret-123'})
    journal.append('unrelated', {'text': 'keep me'})
    erase_expired(journal, {}, source_ids=(packet['sources'][0]['journalEventId'],))
    assert 'private-secret-123' not in journal.path.read_text('utf-8')
    assert journal.entries(types={'unrelated'})[0]['text'] == 'keep me'


@pytest.mark.parametrize('provider_failure', [False, True])
def test_native_inflight_erasure_cannot_restore_response_or_error(tmp_path, provider_failure):
    from egoassist.core.journal import EvidenceUnavailable
    from egoassist.product.answer_service import AnswerService
    async def run():
        service, journal = setup(tmp_path)
        service.ingest('me', journal, [event('a', 'private-secret-123')])
        packet = service.query('me', 'case', 'private-secret')
        answerer = AnswerService('test')
        async def complete(*args, **kwargs):
            erase_expired(SessionJournal(journal.path), {}, source_ids=(packet['sources'][0]['journalEventId'],))
            if provider_failure:
                raise RuntimeError('private-secret-123')
            return {'output': {'choices': [{'message': {'content': 'private-secret-123'}}]}}
        answerer.complete = complete
        with pytest.raises(EvidenceUnavailable):
            await answerer.answer(journal, 'private-secret', packet)
        assert 'private-secret-123' not in journal.path.read_text('utf-8')
        assert not journal.entries(types={'evidence.answer.request', 'evidence.answer.response', 'evidence.answer.failure'})
        await answerer.close()
    asyncio.run(run())


def test_snapshot_survives_new_session_and_unrelated_source_erasure(tmp_path):
    service, journal = setup(tmp_path)
    service.ingest('me', journal, [event(str(i), 'unrelated ' + str(i)) for i in range(20)] + [event('target', 'blue bicycle')])
    packet = service.query('me', 'case', 'bicycle')
    other = SessionJournal(journal.path.with_name('other.jsonl'), session_id='other')
    assert service.query('me', 'case', 'bicycle', snapshot_id=packet['snapshotId']) == packet
    service.ingest('me', other, [event('a', 'other session')])
    # Atomic replacement, including deletion of a non-selected turn, must preserve the packet.
    first = journal.entries(types={'source.event'})[0]
    erase_expired(journal, {}, source_ids=(first['id'],))
    assert service.query('me', 'case', 'bicycle', snapshot_id=packet['snapshotId']) == packet
    erase_expired(other, {}, source_ids=(other.entries(types={'source.event'})[0]['id'],))
    assert service.query('me', 'case', 'bicycle', snapshot_id=packet['snapshotId']) == packet
    erase_expired(journal, {}, source_ids=(packet['sources'][0]['journalEventId'],))
    with pytest.raises(ValueError, match='snapshot unavailable'):
        service.query('me', 'case', 'bicycle', snapshot_id=packet['snapshotId'])


def test_prepared_background_answer_checks_snapshot_before_publication(tmp_path):
    from egoassist.product.answer_jobs import AnswerJobs, answer_jobs
    async def run():
        service, journal = setup(tmp_path)
        service.ingest('me', journal, [event('a', 'blue bicycle')])
        packet = service.query('me', 'case', 'bicycle')
        manager = AnswerJobs('test')
        manager.register('me', journal, turn_id='q', question='bicycle', instructions='',
            context={'evidencePackage': packet}, deferred=True)
        row = manager.transition(journal, answer_jobs(journal)['q'], preparedResult={'text': 'blue'})
        service.invalidate_user('me')
        await manager._execute(journal, row)
        assert answer_jobs(journal)['q']['status'] == 'failed'
        erase_expired(journal, {}, source_ids=(packet['sources'][0]['journalEventId'],))
        await manager._execute(journal, row)
        assert not answer_jobs(journal)
        await manager.close()
    asyncio.run(run())


def test_native_account_removal_does_not_recreate_files(tmp_path):
    import shutil
    from egoassist.core.journal import EvidenceUnavailable
    from egoassist.product.answer_service import AnswerService
    async def run():
        service, journal = setup(tmp_path)
        service.ingest('me', journal, [event('a', 'private-secret-123')])
        packet = service.query('me', 'case', 'private-secret')
        answerer = AnswerService('test')
        account = journal.path.parent.parent
        async def complete(*args, **kwargs):
            shutil.rmtree(account)
            return {'output': {'choices': [{'message': {'content': 'private-secret-123'}}]}}
        answerer.complete = complete
        with pytest.raises(EvidenceUnavailable):
            await answerer.answer(journal, 'private-secret', packet)
        assert not account.exists()
        await answerer.close()
    asyncio.run(run())


def test_legacy_inflight_delete_does_not_restore_audit_or_job(tmp_path):
    from egoassist.product.answer_jobs import AnswerJobs, answer_jobs
    async def run():
        service, journal = setup(tmp_path)
        service.ingest('me', journal, [event('a', 'private-secret-123')])
        packet = service.query('me', 'case', 'private-secret')
        manager = AnswerJobs('test')
        manager.register('me', journal, turn_id='q', question='private-secret', instructions='',
            context={'evidencePackage': packet}, deferred=True)
        async def complete(*args, **kwargs):
            erase_expired(SessionJournal(journal.path), {}, source_ids=(packet['sources'][0]['journalEventId'],))
            return {'output': {'choices': [{'message': {'content': 'private-secret-123'}}]}}
        manager.answer_service.complete = complete
        await manager._execute(journal, answer_jobs(journal)['q'])
        assert not answer_jobs(journal)
        assert 'private-secret-123' not in journal.path.read_text('utf-8')
        assert not journal.entries(types={'answer.request', 'answer.result'})
        await manager.close()
    asyncio.run(run())


def test_new_source_indexing_does_not_scan_passages_for_replacement(tmp_path, monkeypatch):
    import sqlite3
    service, journal = setup(tmp_path)
    statements = []
    connect = sqlite3.connect
    def traced(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db
    monkeypatch.setattr(sqlite3, 'connect', traced)
    service.ingest('me', journal, [event('a', 'Blue bicycle'), event('b', 'Green bicycle')])
    assert not any(sql.startswith('DELETE FROM passages WHERE source=') for sql in statements)
    packet = service.query('me', 'case', 'bicycle')
    assert {row['externalId'] for row in packet['sources']} == {'a', 'b'}
    # A repeated journal event must still replace its passages instead of duplicating them.
    original = journal.entries(types={'source.event'})[0]
    journal.append('source.event', {'id': original['id'], 'source': original['source']})
    statements.clear()
    service.index('me').sync()
    assert any(sql.startswith('DELETE FROM passages WHERE source=') for sql in statements)
    assert service.query('me', 'case', 'bicycle')['coverage']['matchingPassages'] == 2
