import asyncio
from datetime import datetime, timezone
import hashlib
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.diary.generator import DiaryGenerator, SECTIONS, validate
from egoassist.diary.service import DiaryService
from egoassist.diary.sources import collect, chunks, period_for
from egoassist.diary.store import DiaryStore
from egoassist.memory import TimelineMemory
from run_egoassist import create_app


def body(ids, text='测试归纳，不是真实模型输出'):
    return {'title':'测试回顾',**{key:[] for key in SECTIONS},
            'overview':[{'text':text,'sourceIds':list(ids)[:12]}]}


def message(journal, text, stamp='2026-09-01T10:00:00+08:00'):
    return journal.append('message',{'timestamp':stamp,'status':'completed','message':{'role':'user','content':text}})


def test_diary_reads_three_lanes_without_mutating_sources_and_respects_owner(tmp_path):
    root=tmp_path/'me';journal=SessionJournal(root/'sessions'/'case.jsonl',session_id='case')
    message(journal,'计划整理资料')
    journal.append('ambient_observation',{'timestamp':'2026-09-01T10:00:01+08:00','status':'accepted',
        'evidence':{'capturedAt':'2026-09-01T10:00:00+08:00','visibleEvidence':['桌上有书'],'uncertainties':['未看见封面']}})
    journal.append('tool_result',{'timestamp':'2026-09-01T11:00:00+08:00','name':'test','result':{'done':False},'isError':True})
    timeline=TimelineMemory(tmp_path/'timeline.sqlite3')
    a=timeline.remember('最初约周三',user_id='me',case_id='case',scope='case',kind='event',happened_at='2026-09-01T10:00:00+08:00')
    timeline.remember('不属于我',user_id='other',case_id='other',scope='case',kind='event',happened_at='2026-09-01T10:00:00+08:00')
    before=hashlib.sha256(journal.path.read_bytes()).hexdigest()
    data=collect(root,timeline.path,'me',period_for('daily','2026-09-01','Asia/Shanghai'))
    assert {r['kind'] for r in data['records']}=={'user-statement','sampled-observation','execution-receipt','memory-state'}
    assert '不属于我' not in str(data)
    assert before==hashlib.sha256(journal.path.read_bytes()).hexdigest()
    timeline.delete(a['id'])
    after=collect(root,timeline.path,'me',period_for('daily','2026-09-01','Asia/Shanghai'))
    assert not any(r['kind']=='memory-state' for r in after['records'])
    assert after['fingerprint']!=data['fingerprint']


def test_month_chunks_cover_every_day_and_long_record_without_tail_loss(tmp_path):
    root=tmp_path/'me';path=root/'sessions'/'case.jsonl';path.parent.mkdir(parents=True)
    rows=[{'type':'message','id':f'{day}-{i}','timestamp':f'2026-09-{day:02d}T10:00:00+08:00',
           'status':'completed','message':{'role':'user','content':'事项 '*100}} for day in range(1,31) for i in range(100)]
    rows[0]['message']['content']='完整长记录'*5000
    path.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
    data=collect(root,tmp_path/'absent.sqlite3','me',period_for('monthly','2026-09-17','Asia/Shanghai'))
    batches=list(chunks(data['records']))
    assert len(data['coverage']['byDay'])==30 and data['coverage']['records']==3000
    assert {r['sourceId'] for batch in batches for r in batch}=={r['sourceId'] for r in data['records']}
    first=data['records'][0]
    assert ''.join(r['text'] for batch in batches for r in batch if r['sourceId']==first['sourceId'])==first['text']


def test_hierarchical_pipeline_processes_all_chunks_and_reuses_checkpoints():
    async def run():
        generator=DiaryGenerator('unused');seen=[]
        async def call(stage,period,inputs,allowed):
            seen.append((stage,allowed));return body(allowed),{'stage':stage,'providerRequestId':'deterministic-test'}
        generator.call=call
        records=[{'sourceId':f'd{day}-{i}','localDate':f'2026-09-{day:02d}','text':'来源内容'*400} for day in range(1,8) for i in range(20)]
        row={'period':period_for('weekly','2026-09-01','Asia/Shanghai')}
        result,daily=await generator.generate(row,{'records':records},lambda row:None)
        processed=set().union(*(refs for stage,refs in seen if '整理分块' in stage))
        assert processed=={r['sourceId'] for r in records} and len(daily)==7
        count=len(seen)
        await generator.generate(row,{'records':records},lambda row:None)
        assert len(seen)==count
        validate(result,{r['sourceId'] for r in records})
    asyncio.run(run())


def test_diary_rejects_fabricated_refs_and_excess_claims():
    with pytest.raises(ValueError): validate(body(['invented']),{'actual'})
    invalid=body(['actual']);invalid['suggestions']=[{'text':'do','sourceIds':['actual']}]*4
    with pytest.raises(ValueError): validate(invalid,{'actual'})


def test_diary_archive_pagination_notes_and_feedback_survive_reopen(tmp_path):
    store=DiaryStore(tmp_path)
    ids=[]
    for day in range(1,21):
        row=store.create(period_for('daily',f'2026-09-{day:02d}','Asia/Shanghai'))
        row.update(status='completed',body=body(['x']));store.put(row);ids.append(row['id'])
    store.note('2026-09-02','个人补记');store.feedback(ids[0],0,'done')
    reopened=DiaryStore(tmp_path)
    page=reopened.list();next_page=reopened.list(before=page['next'])
    assert len(page['entries'])==12 and len(next_page['entries'])==8
    assert len({r['id'] for r in page['entries']+next_page['entries']})==20
    assert reopened.notes('2026-09-01','2026-10-01')[0]['text']=='个人补记'
    assert reopened.feedback(ids[0])=={'0':'done'}
    filtered=reopened.list(kind='daily',period=period_for('daily','2026-09-02','Asia/Shanghai'))
    assert [r['id'] for r in filtered['entries']]==[ids[1]]


def test_diary_deleted_sources_invalidate_saved_report_and_checkpoints(tmp_path):
    async def run():
        root=tmp_path/'users'/'me';journal=SessionJournal(root/'sessions'/'case.jsonl',session_id='case')
        message(journal,'将要删除的来源')
        service=DiaryService(tmp_path/'users',tmp_path/'timeline.sqlite3','')
        store=service.store('me');row=store.create(period_for('daily','2026-09-01','Asia/Shanghai'))
        data=service.sources('me',row['period'])
        row.update(status='completed',sourceFingerprint=data['fingerprint'],body=body([data['records'][0]['sourceId']]),
                   sources=data['records'],nodes={'x':'private'},calls=[{'rawOutput':'private'}])
        store.put(row);journal.path.unlink()
        result=await service.get('me',row['id'])
        assert result['status']=='stale' and not {'body','sources','nodes','calls'} & result.keys()
        assert '将要删除的来源' not in str(store.get(row['id']))
    asyncio.run(run())


def test_diary_restart_recovers_and_cancel_does_not_touch_lane(tmp_path):
    async def run():
        root=tmp_path/'users'/'me';journal=SessionJournal(root/'sessions'/'case.jsonl',session_id='case')
        message(journal,'原始资料')
        snapshot=journal.path.read_bytes();service=DiaryService(tmp_path/'users',tmp_path/'none.sqlite3','')
        started=asyncio.Event()
        async def pending(row,data,save): started.set();await asyncio.Future()
        service.generator.generate=pending
        row=service.submit('me',period_for('daily','2026-09-01','Asia/Shanghai'))
        await asyncio.wait_for(started.wait(),3);await service.close()
        assert service.store('me').get(row['id'])['status']=='queued'
        restored=DiaryService(tmp_path/'users',tmp_path/'none.sqlite3','')
        async def complete(row,data,save): return body([data['records'][0]['sourceId']]),[]
        restored.generator.generate=complete;await restored.tick()
        await asyncio.gather(*list(restored.tasks.values()))
        assert restored.store('me').get(row['id'])['status']=='completed'
        assert journal.path.read_bytes()==snapshot
        await restored.close()
    asyncio.run(run())


def test_diary_api_account_scope_and_original_reports_remain_available(tmp_path):
    async def run():
        async with TestClient(TestServer(create_app(str(BUILTIN_SKILLS_ROOT),None,str(tmp_path/'runtime.json'),'unused','Ethan'))) as client:
            assert (await client.get('/api/diary')).status==401
            await client.post('/api/auth/register',json={'username':'diary-one','password':'diary-password-123'})
            assert (await client.get('/api/diary')).status==200
            sid=(await (await client.post('/api/sessions')).json())['session']['id']
            assert (await client.get('/api/insights',params={'session':sid})).status==200
            accepted=await client.post('/api/diary',json={'kind':'daily','anchor':'2020-01-01','timezone':'Asia/Shanghai'})
            assert accepted.status==202
            identifier=(await accepted.json())['id']
            async with asyncio.timeout(5):
                while True:
                    row=await (await client.get('/api/diary/'+identifier)).json()
                    if row['status'] not in {'queued','running'}:break
                    await asyncio.sleep(.02)
            assert row['status']=='empty'
            await client.post('/api/sessions')
            assert (await (await client.get('/api/diary')).json())['entries'][0]['id']==identifier
            assert (await client.get('/api/diary/'+identifier+'/export')).status==200
            await client.post('/api/auth/logout')
            await client.post('/api/auth/register',json={'username':'diary-two','password':'diary-password-123'})
            assert (await client.get('/api/diary/'+identifier)).status==400
            assert not (await (await client.get('/api/diary')).json())['entries']
    asyncio.run(run())


def test_diary_can_be_disabled_without_disabling_old_reports(tmp_path,monkeypatch):
    monkeypatch.setenv('EGOASSIST_DIARY_ENABLED','0')
    app=create_app(str(BUILTIN_SKILLS_ROOT),None,str(tmp_path/'runtime.json'),'unused','Ethan')
    paths={r.resource.canonical for r in app.router.routes()}
    assert '/api/diary' not in paths and '/api/insights' in paths and '/ws/realtime' in paths


def test_diary_job_projection_excludes_router_prompts_but_keeps_outcome(tmp_path):
    journal=SessionJournal(tmp_path/'sessions'/'case.jsonl',session_id='case')
    stamp='2026-09-01T10:00:00+08:00'
    journal.append('intent_envelope',{'timestamp':stamp,'intentId':'intent','payload':{'semantic':{'prompt':'PRIVATE_PROMPT'}}})
    journal.append('job_ledger',{'timestamp':stamp,'jobId':'job','intentId':'intent','kind':'background-job','payload':{'text':'检查资料'}})
    journal.append('job_state',{'timestamp':stamp,'jobId':'job','status':'failed','detail':{'error':'原件打不开','runtimeCheckpoint':{'prompt':'PRIVATE_PROMPT'}}})
    data=collect(tmp_path,tmp_path/'none.sqlite3','me',period_for('daily','2026-09-01','Asia/Shanghai'))
    text=data['records'][0]['text']
    assert 'PRIVATE_PROMPT' not in text and '原件打不开' in text and '检查资料' in text


def test_diary_assistant_delivery_is_distinct_from_user_fact(tmp_path):
    journal=SessionJournal(tmp_path/'sessions'/'case.jsonl',session_id='case')
    message(journal,'帮我写周报')
    journal.append('message',{'timestamp':'2026-09-01T10:01:00+08:00','status':'completed',
        'message':{'role':'assistant','content':'这是周报草稿，不表示现实任务完成'}})
    data=collect(tmp_path,tmp_path/'none.sqlite3','me',period_for('daily','2026-09-01','Asia/Shanghai'))
    assert [r['kind'] for r in data['records']]==['user-statement','assistant-delivery']


def test_diary_schedule_is_opt_in_and_deduplicates_closed_periods(tmp_path):
    async def run():
        (tmp_path/'users'/'me').mkdir(parents=True)
        service=DiaryService(tmp_path/'users',tmp_path/'none.sqlite3','')
        store=service.store('me');await service.tick();assert not service.tasks
        store.setting('schedule',{'enabled':True,'timezone':'Asia/Shanghai','kinds':['daily','weekly','monthly']})
        await service.tick();await asyncio.gather(*list(service.tasks.values()))
        rows=store.list()['entries'];assert len(rows)==3
        assert all(r['status']=='empty' for r in rows)
        await service.tick();assert len(store.list()['entries'])==3
        await service.close()
    asyncio.run(run())


def test_diary_notes_export_and_retention_invalidate_only_diary_derivatives(tmp_path):
    async def run():
        async with TestClient(TestServer(create_app(str(BUILTIN_SKILLS_ROOT),None,str(tmp_path/'runtime.json'),'unused','Ethan'))) as client:
            auth=await (await client.post('/api/auth/register',json={'username':'diary-privacy','password':'diary-password-123'})).json()
            uid=auth['user']['id'];await client.get('/api/diary')
            assert (await client.post('/api/diary/notes',json={'day':'2026-09-01','text':'补记，尚未完成验收'})).status==201
            store=DiaryStore(tmp_path/'users'/uid)
            row=store.create(period_for('daily','2026-09-01','Asia/Shanghai'))
            row.update(status='completed',body=body(['note:test']),calls=[{'rawOutput':'old-derived-secret'}])
            store.put(row)
            exported=await (await client.get('/api/privacy/export')).json()
            assert exported['personalDiary']['notes'][0]['text']=='补记，尚未完成验收'
            result=await client.post('/api/privacy/retention',json={'category':'transcript','retentionDays':1,'applyNow':True})
            assert result.status==200
            assert store.get(row['id'])['status']=='stale'
            assert 'old-derived-secret' not in str(store.get(row['id']))
            assert store.notes('2026-09-01','2026-09-02')
            deleted=await client.post('/api/privacy/delete',json={'confirm':True})
            assert deleted.status==200 and not (tmp_path/'users'/uid).exists()
    asyncio.run(run())


def test_diary_snapshot_completes_while_new_records_arrive_and_remains_readable(tmp_path):
    async def run():
        root=tmp_path/'users'/'me';journal=SessionJournal(root/'sessions'/'case.jsonl',session_id='case')
        original=message(journal,'上午已经完成原型检查')
        service=DiaryService(tmp_path/'users',tmp_path/'none.sqlite3','')
        started,release=asyncio.Event(),asyncio.Event()
        async def generate(row,data,save):
            started.set();await release.wait()
            return body([data['records'][0]['sourceId']],'上午完成原型检查'),[]
        service.generator.generate=generate
        row=service.submit('me',period_for('daily','2026-09-01','Asia/Shanghai'))
        await started.wait()
        message(journal,'下午新增计划','2026-09-01T14:00:00+08:00')
        running=await service.get('me',row['id'])
        assert running['status']=='running' and running['newRecords']==1
        release.set();await asyncio.gather(*list(service.tasks.values()))
        completed=await service.get('me',row['id'])
        assert completed['status']=='completed' and completed['newRecords']==1
        assert len(completed['sources'])==1 and completed['snapshotAt']
        service.store('me').note('2026-09-01','新增补记')
        reopened=DiaryService(tmp_path/'users',tmp_path/'none.sqlite3','')
        result=await reopened.get('me',row['id'])
        assert result['status']=='completed' and result['newRecords']==2
        assert result['body']['overview'][0]['text']=='上午完成原型检查'
        # Removal of evidence must still retract body and checkpoints.
        lines=journal.path.read_text('utf-8').splitlines()
        journal.path.write_text('\n'.join(line for line in lines if json.loads(line)['id']!=original['id'])+'\n',encoding='utf-8')
        invalid=await reopened.get('me',row['id'])
        assert invalid['status']=='stale' and not {'body','sources','nodes','calls'} & invalid.keys()
        await service.close();await reopened.close()
    asyncio.run(run())


def test_diary_snapshot_ignores_index_touch_but_retracts_changed_evidence(tmp_path):
    from egoassist.diary.sources import source_manifest,snapshot_change
    source={'sourceId':'memory:one','kind':'memory-state','text':'原始事实','updatedAt':'old'}
    original={'records':[source],'coverage':{'complete':True}}
    row={'sourceManifest':source_manifest(original)}
    touched={'records':[{**source,'updatedAt':'new','derivedEventHints':[{'title':'new index'}]}], 'coverage':{'complete':True}}
    assert snapshot_change(row,touched)=={'invalid':False,'added':0}
    touched['records'][0]['text']='已修正事实'
    assert snapshot_change(row,touched)['invalid']
    assert snapshot_change(row,{'records':[source],'coverage':{'complete':False}})['invalid']
    row={'sourceManifest':{'case:original':'hash'},'sourceExternalRefs':{'case:external-id':'case:original'}}
    data={'records':[{'sourceId':'case:original','kind':'historical-dialogue','text':'{}'},
        {'sourceId':'case:correction','sessionId':'case','kind':'historical-dialogue','text':json.dumps({'supersedes':'external-id'})}],
        'coverage':{'complete':True}}
    row['sourceManifest']['case:original']=source_manifest(data)['case:original']
    assert snapshot_change(row,data)['invalid']


def test_diary_notes_list_is_account_scoped_and_date_filtered(tmp_path):
    async def run():
        async with TestClient(TestServer(create_app(str(BUILTIN_SKILLS_ROOT),None,str(tmp_path/'runtime.json'),'unused','Ethan'))) as client:
            assert (await client.get('/api/diary/notes?day=2026-09-01')).status==401
            await client.post('/api/auth/register',json={'username':'notes-one','password':'notes-password-123'})
            await client.post('/api/diary/notes',json={'day':'2026-09-01','text':'已保存补记'})
            result=await (await client.get('/api/diary/notes?day=2026-09-01')).json()
            assert [row['text'] for row in result['notes']]==['已保存补记']
            assert not (await (await client.get('/api/diary/notes?day=2026-09-02')).json())['notes']
            await client.post('/api/auth/logout')
            await client.post('/api/auth/register',json={'username':'notes-two','password':'notes-password-123'})
            assert not (await (await client.get('/api/diary/notes?day=2026-09-01')).json())['notes']
    asyncio.run(run())
