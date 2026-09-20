import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import pytest

from egoassist.memory import TimelineMemory
from egoassist.realtime.router_context import retrieval_text
from egoassist.realtime.visual_history import select_history, validate_evidence
from egoassist.realtime.scene import SharedScene
from egoassist.core.journal import SessionJournal
from egoassist.eval.choice import explicit_choice


def observation(i, text='桌面上的物品', recorded=None):
    at=f'2026-09-16T10:{i//60:02d}:{i%60:02d}+00:00'
    return {'id':str(i),'status':'accepted','timestamp':recorded or at,
            'evidence':{'capturedAt':at,'visibleEvidence':[text],
                        'media':{'sha256':str(i),'assetId':f'{i}.jpg'}}}


def test_older_relevant_frame_survives_many_later_observations():
    rows=[observation(i, '小马形座椅替代三角支架' if i==12 else '人在房间内看电脑') for i in range(160)]
    found=select_history(rows,'三角支架被什么替代',before='2026-09-16T11:00:00+00:00')
    ids={x['sourceId'] for x in found}
    assert '12' in ids and '0' in ids and '159' in ids
    assert len(found)<=24
    assert [x['capturedAt'] for x in found]==sorted(x['capturedAt'] for x in found)


def test_future_late_commit_rejected_duplicate_and_empty_limit():
    future='2026-09-16T12:00:00+00:00'
    rows=[observation(1),observation(2,recorded=future),observation(3)]
    rows[2]['evidence']['capturedAt']=future
    rejected=observation(4);rejected['status']='rejected';rows.append(rejected)
    rows.append(deepcopy(rows[0]))
    assert [x['sourceId'] for x in select_history(rows,'物品',before='2026-09-16T11:00:00+00:00')]==['1']
    assert select_history(rows,'物品',before=future,limit=0)==[]


def test_history_orders_instants_across_timezone_offsets():
    first, second = observation(1), observation(2)
    first['evidence']['capturedAt'] = '2026-09-16T10:00:00+08:00'
    second['evidence']['capturedAt'] = '2026-09-16T03:00:00+00:00'
    found = select_history([second, first], '物品', before='2026-09-16T11:00:00+00:00')
    assert [row['sourceId'] for row in found] == ['1', '2']


def test_query_projection_requires_single_verbatim_intent():
    transcript='请按要求回答。钥匙之前放在哪里？输出一句话。'
    semantic={'intents':[{'kind':'foreground-dialogue','text':'钥匙之前放在哪里？'}]}
    assert retrieval_text(transcript,semantic)=='钥匙之前放在哪里？'
    semantic['intents'][0]['text']='身份证之前放在哪里？'
    assert retrieval_text(transcript,semantic)==transcript
    semantic['intents']*=2
    assert retrieval_text(transcript,semantic)==transcript


def test_ranking_query_cannot_remove_owner_or_time_constraints(tmp_path):
    memory=TimelineMemory(tmp_path/'timeline.sqlite3')
    for user,date in [('me','2026-09-14'),('other','2026-09-15')]:
        memory.remember('钥匙放在黄色抽屉',user_id=user,case_id='case',scope='case',kind='event',
                        happened_at=date+'T10:00:00+00:00')
    result=memory.retrieve('2026-09-15 钥匙在哪里',ranking_query='钥匙 黄色抽屉',user_id='me',case_id='case',scope='case',
                           now=datetime(2026,9,16,tzinfo=timezone.utc),min_score=0)
    assert result['selected']==[]
    assert all(c['card']['userId']=='me' for c in result['candidates'])


def test_history_is_opt_in_and_disabled_without_memory_permission(tmp_path):
    scene=SharedScene(SessionJournal(tmp_path/'case.jsonl',session_id='case'),'test-key')
    scene.capture('q',explicit=True)
    calls=[]
    async def observe(*args):calls.append(args);return {'status':'observed','evidence':[]}
    scene.visual_history.observe=observe
    async def check():
        no=await scene.context('q',describe_frame=True,question='之前的桌子？')
        assert no['historicalVisual'] is None and calls==[]
        yes=await scene.context('q',describe_frame=True,question='之前的桌子？',allow_history=True)
        assert yes['historicalVisual']['status']=='observed' and len(calls)==1
        await scene.close()
    asyncio.run(check())


def test_retracted_choice_must_not_be_scored_as_correct():
    assert explicit_choice('选择：B。理由是看见小马座椅。')=='B'
    assert explicit_choice('选择：B. 但正确答案应为 D. 没有代替。') is None
    assert explicit_choice('选择：B\n修正：选择：无法确定') is None
    assert explicit_choice('选择：无法确定') is None


@pytest.mark.parametrize('value', [
    [], {'evidence': [], 'uncertainties': 'unknown'},
    {'evidence': ['invented'], 'uncertainties': []},
    {'evidence': [{'description': 'object', 'sourceIds': ['other-case']}], 'uncertainties': []},
    {'evidence': [{'description': 'object', 'sourceIds': 'known'}], 'uncertainties': []},
    {'evidence': [{'description': '', 'sourceIds': ['known']}], 'uncertainties': []},
])
def test_invalid_model_claims_cannot_enter_answer_context(value):
    with pytest.raises(ValueError):
        validate_evidence(value, {'known'})


def test_missing_historical_media_does_not_fall_back_to_unverified_caption(tmp_path):
    journal = SessionJournal(tmp_path/'case.jsonl', session_id='case')
    row = observation(1)
    journal.append('ambient_observation', {'status': 'accepted', 'evidence': row['evidence']})
    scene = SharedScene(journal, 'test-key')
    scene.capture('q', explicit=True)
    async def check():
        result = await scene.visual_history.observe('q', '桌上有什么')
        assert result['status'] == 'unavailable'
        assert 'evidence' not in result
        assert journal.entries(types={'scene.history.result'})[-1]['status'] == 'unavailable'
        await scene.close()
    asyncio.run(check())


def test_historical_request_cancellation_is_logged_and_propagated(tmp_path):
    journal = SessionJournal(tmp_path/'case.jsonl', session_id='case')
    scene = SharedScene(journal, 'test-key')
    ref = scene._save(b'test-media', '.jpg', 'image/jpeg')
    journal.append('ambient_observation', {'status': 'accepted', 'evidence': {
        'capturedAt': datetime.now(timezone.utc).isoformat(), 'media': ref, 'visibleEvidence': ['table']}})
    scene.capture('q', explicit=True)
    class CancelledRequest:
        async def __aenter__(self): raise asyncio.CancelledError
        async def __aexit__(self, *args): return False
    class Client:
        def post(self, *args, **kwargs): return CancelledRequest()
    scene.http = lambda: Client()
    async def check():
        with pytest.raises(asyncio.CancelledError):
            await scene.visual_history.observe('q', 'table')
        assert len(journal.entries(types={'scene.history.cancelled'})) == 1
        assert not journal.entries(types={'scene.history.result'})
        await scene.close()
    asyncio.run(check())
