import asyncio
from copy import deepcopy
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.diary.sources import collect, period_for
from egoassist.realtime.long_events import LongEvents, projection, pending, next_batch, validate_updates
from run_egoassist import create_app


def observe(journal, text, stamp):
    return journal.append('ambient_observation', {'status':'accepted', 'timestamp':stamp,
        'evidence':{'capturedAt':stamp, 'visibleEvidence':[text], 'uncertainties':[]}})


def update(refs, eid=None, status='ongoing', phase='准备食材'):
    return {'eventId':eid, 'title':'做晚饭', 'summary':'根据采样记录整理做饭过程', 'phase':phase,
            'status':status, 'sourceIds':refs, 'uncertainties':['未连续观察']}


def commit(journal, changes):
    state=projection(journal); batch=pending(state)
    result=validate_updates({'updates':changes}, state, batch)
    journal.append('long_event.batch', {'events':result, 'processedSourceIds':[s['sourceId'] for s in batch]})
    return result


def test_long_event_stages_gaps_restart_and_derived_diary_hints(tmp_path):
    root=tmp_path/'me'; journal=SessionJournal(root/'sessions'/'case.jsonl', session_id='case')
    a=observe(journal,'台面有切好的蔬菜','2026-09-01T10:00:00+08:00')
    eid=commit(journal,[update([a['id']])])[0]['eventId']
    b=observe(journal,'正在锅中翻炒','2026-09-01T10:10:00+08:00')
    c=journal.append('message', {'timestamp':'2026-09-01T10:11:00+08:00','status':'completed',
        'message':{'role':'user','content':'先暂停做饭，我要接电话'}})
    commit(journal,[update([b['id'],c['id']],eid,'paused','翻炒后用户表示暂停做饭')])
    tracker=LongEvents(SessionJournal(journal.path),'')
    saved=tracker.snapshot()['events'][0]
    assert saved['eventId']==eid and len(saved['phases'])==2
    assert saved['coverage']['continuousDurationSeconds'] is None
    assert saved['coverage']['evidenceSpanSeconds']==660
    assert saved['coverage']['gaps'] and saved['status']=='paused'
    before=journal.path.read_bytes()
    diary=collect(root,tmp_path/'absent.sqlite3','me',period_for('daily','2026-09-01','Asia/Shanghai'))
    assert len(diary['records'])==3
    assert all(r['derivedEventHints'][0]['eventId']==eid for r in diary['records'])
    assert journal.path.read_bytes()==before
    # Reset removes derived hints, never the raw sources.
    journal.append('long_event.reset',{})
    assert not tracker.snapshot()['events'] and tracker.snapshot()['pendingSources']==3
    assert all('derivedEventHints' not in r for r in collect(root,tmp_path/'absent.sqlite3','me',diary_period())['records'])
    asyncio.run(tracker.close())


def diary_period():
    return period_for('daily','2026-09-01','Asia/Shanghai')


def test_event_rejects_fabricated_citations_and_closed_occurrence_reuse(tmp_path):
    journal=SessionJournal(tmp_path/'case.jsonl')
    a=observe(journal,'饭菜端上桌','2026-09-01T10:00:00+08:00')
    with pytest.raises(ValueError,match='citations'):
        commit(journal,[update(['invented'])])
    eid=commit(journal,[update([a['id']],status='completed')])[0]['eventId']
    b=observe(journal,'又拿出蔬菜','2026-09-01T11:00:00+08:00')
    with pytest.raises(ValueError,match='new occurrence'):
        commit(journal,[update([b['id']],eid)])
    assert len(commit(journal,[update([b['id']])]))==1


def test_no_future_summary_or_erased_source_in_answer_context(tmp_path):
    journal=SessionJournal(tmp_path/'case.jsonl')
    a=observe(journal,'食材','2026-09-01T10:00:00+08:00')
    commit(journal,[update([a['id']])])
    tracker=LongEvents(journal,'')
    assert not tracker.snapshot(before='2026-09-01T10:00:01+08:00')['events']
    journal.rewrite(lambda rows:[r for r in rows if r['id']!=a['id']])
    assert not tracker.snapshot()['events']
    asyncio.run(tracker.close())


def test_failed_generation_keeps_pending_and_success_is_idempotent(tmp_path):
    async def run():
        journal=SessionJournal(tmp_path/'case.jsonl')
        a=observe(journal,'食材','2026-09-01T10:00:00+08:00')
        tracker=LongEvents(journal,'test');calls=[]
        async def complete(*args,**kwargs):
            calls.append(1)
            if len(calls)==1: raise TimeoutError('test outage')
            return {'output':{'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'updates':[update([a['id']])]})}}]}, 'request_id':'test-request'}
        tracker.service.complete=complete
        await tracker.run()
        assert tracker.snapshot()['pendingSources']==1 and tracker.snapshot()['job']['status']=='failed'
        await tracker.run()
        await tracker.run()
        assert len(calls)==2 and tracker.snapshot()['pendingSources']==0
        assert len(tracker.snapshot()['events'])==1
        await tracker.close()
    asyncio.run(run())


def test_batches_do_not_bridge_long_recording_gap_and_evidence_paginates(tmp_path):
    journal=SessionJournal(tmp_path/'case.jsonl')
    first=observe(journal,'桌面操作','2026-09-01T10:00:00+08:00')
    observe(journal,'再次看到桌面','2026-09-01T12:00:00+08:00')
    assert [s['sourceId'] for s in next_batch(projection(journal))]==[first['id']]
    event=commit(journal,[update([first['id']])])[0]
    tracker=LongEvents(journal,'')
    assert tracker.evidence(event['eventId'],limit=1)['sources'][0]['text']=='桌面操作'
    assert tracker.evidence(event['eventId'],limit=1)['nextOffset'] is None
    with pytest.raises(ValueError):tracker.evidence('not-owned')
    asyncio.run(tracker.close())


def test_old_only_update_is_noop_not_unsupported_new_stage(tmp_path):
    journal=SessionJournal(tmp_path/'case.jsonl')
    first=observe(journal,'食材','2026-09-01T10:00:00+08:00')
    event=commit(journal,[update([first['id']])])[0]
    observe(journal,'新画面','2026-09-01T10:01:00+08:00')
    assert commit(journal,[update([first['id']],event['eventId'],'completed')])==[]
    assert projection(journal)['events'][event['eventId']]['status']=='ongoing'


def test_erasing_raw_evidence_cascades_to_event_and_answer(tmp_path):
    from egoassist.core.retention import erase_expired
    journal=SessionJournal(tmp_path/'case.jsonl')
    a=observe(journal,'食材','2026-09-01T10:00:00+08:00')
    e=commit(journal,[update([a['id']])])[0]
    answer=journal.append('answer.job',{'turnId':'question','context':{'eventId':e['eventId']},'text':'食材'})
    erase_expired(journal,{},source_ids={a['id']})
    assert not projection(journal)['events']
    assert next(r for r in journal.entries() if r['id']==answer['id'])['type']=='retention.deleted'


def test_automatic_failures_stop_at_three_but_manual_retry_remains_possible(tmp_path):
    async def run():
        journal=SessionJournal(tmp_path/'case.jsonl')
        observe(journal,'桌上食材','2026-09-01T10:00:00+08:00')
        tracker=LongEvents(journal,'test')
        for _ in range(3):journal.append('long_event.job',{'status':'failed'})
        tracker.schedule()
        assert tracker.task is None and tracker.snapshot()['automaticRetryPaused']
        called=[]
        async def worker():called.append(True)
        tracker._worker=worker
        tracker.schedule(force=True)
        await tracker.task
        assert called==[True]
        await tracker.close()
    asyncio.run(run())


def test_speaker_merge_and_event_endpoints_require_own_session(tmp_path):
    async def run():
        app=create_app(str(BUILTIN_SKILLS_ROOT),None,str(tmp_path/'runtime.json'),'test','Ethan')
        async with TestClient(TestServer(app)) as client:
            for endpoint in ('/api/scene/events','/api/scene/speakers/merge'):
                assert (await client.post(endpoint,json={})).status==401
            await client.post('/api/auth/register',json={'username':'owner','password':'valid-password-123'})
            sid=(await (await client.post('/api/sessions')).json())['session']['id']
            await client.post('/api/auth/register',json={'username':'other','password':'valid-password-456'})
            for endpoint,action in (('/api/scene/events','rebuild'),('/api/scene/speakers/merge','merge')):
                response=await client.post(endpoint,json={'sessionId':sid,'action':action})
                assert response.status in {400,403}
    asyncio.run(run())
