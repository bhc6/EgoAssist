import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.core.retention import erase_expired
from egoassist.prompts import compact_audit_payload
from egoassist.realtime.model_audit import page, detail, record_request, record_output
from run_egoassist import create_app


def provider(journal, kind, **data):
    return journal.append('runtime_event', {'eventType': 'provider.model.' + kind, 'payload': data})


def test_interleaved_calls_exact_inputs_partial_failure_and_rewrite(tmp_path):
    journal = SessionJournal(tmp_path / 'case.jsonl', session_id='case')
    a = provider(journal, 'request', callId='a', model='text', stage='text', responseId='ra', request={'messages':['old context']})
    b = provider(journal, 'request', callId='b', model='text', stage='text', responseId='rb', request={'messages':['new context']})
    provider(journal, 'response', callId='b', output='new answer', status='completed')
    provider(journal, 'response', callId='a', output='partial old answer', status='failed', error='TimeoutError')
    old = detail(journal, a['id'])
    assert old['inputs'][0]['content'] == {'messages':['old context']}
    assert old['outputs'][0]['content'] == 'partial old answer' and old['status'] == 'failed'
    assert old['responseId'] == 'ra' and old['error'] == 'TimeoutError'
    assert detail(journal, b['id'])['outputs'][0]['content'] == 'new answer'
    listing = page(journal, limit=1)
    assert listing['models'][0]['id'] == b['id']  # Late old output cannot replace the latest call.
    assert len(listing['calls']) == 1 and listing['nextBefore']
    assert len(page(journal, before=listing['nextBefore'])['calls']) == 1
    assert 'old context' not in json.dumps(listing)
    assert not page(journal, model='unknown')['calls']
    with pytest.raises(ValueError): page(journal, before='not-a-cursor')
    with pytest.raises(ValueError): page(journal, limit=101)
    # Rewrite invalidates the catalogue; erased input must not survive in cache.
    journal.rewrite(lambda rows: [row for row in rows if row['id'] != a['id']])
    with pytest.raises(ValueError): detail(journal, a['id'])


def test_visual_body_is_exact_and_legacy_chronological_parent_is_not_a_join(tmp_path):
    journal = SessionJournal(tmp_path / 'case.jsonl', session_id='case')
    request = journal.append('scene.request', {'model':'vision', 'frame':{'frameId':'frame'}, 'prompt':'old prompt'})
    legacy = journal.append('scene.result', {'model':'vision', 'description':'unattributed'})
    assert detail(journal, legacy['id'])['inputStatus'] == 'missing'
    assert detail(journal, request['id'])['inputStatus'] == 'partial'
    body = {'model':'vision','input':{'messages':[{'role':'user','content':[{'image':'data:image/jpeg;base64,AAAA'}]}]}}
    assert record_request(journal, request, body) == body
    record_output(journal, request, {'output':{'choices':[{'message':{'content':'invalid JSON'}}]}})
    journal.append('scene.result', {'requestEventId':request['id'], 'status':'unavailable','error':'ValueError'})
    call = detail(journal, request['id'])
    assert call['inputStatus'] == call['outputStatus'] == 'recorded'
    assert call['status'] == 'unavailable' and call['inputs'][-1]['content'] == body
    assert 'invalid JSON' in json.dumps(call['outputs'])
    erase_expired(journal, {}, source_ids=[request['id']])
    assert 'invalid JSON' not in journal.path.read_text(encoding='utf8')


def test_decisions_keep_actual_state_and_separate_review_model(tmp_path):
    journal = SessionJournal(tmp_path / 'case.jsonl', session_id='case')
    request = {'state':'the actual private context', 'questions':{'route':{'criteria':{'a':'reply'}}}}
    row = journal.append('control_decision', {'decision':{'status':'classified','model':'structured', 'rawOutput':'result',
        'requestMetrics':{'messages':[{'role':'system','content':'parser'}],
        'decisions':{'model':'jev','request':request,'rawResponse':{'answers':{'route':'a'}}},
        'taskControlCheck':{'model':'review','request':{'state':'review context'},'error':'TimeoutError'}}}})
    listing = page(journal)
    assert {c['model'] for c in listing['models']} == {'jev','structured','review'}
    assert detail(journal, row['id']+':decisions')['inputs'][0]['content'] == request
    failed = detail(journal, row['id']+':taskControlCheck')
    assert failed['status'] == 'failed' and failed['outputStatus'] == 'missing'


def test_polling_omits_heavy_media():
    for kind, field in [('provider.model.request','request'), ('provider.model.response','output'),
                        ('model.request.body','body'), ('model.response.body','body')]:
        assert 'sensitive-media' not in json.dumps(compact_audit_payload(kind, {field:'sensitive-media', 'model':'m'}))


@pytest.mark.parametrize('stage,category', [('asr', 'transcript'), ('tts', 'transcript'), ('vision', 'observation'), ('text', 'operational')])
def test_raw_provider_audit_expires_with_content_and_removes_later_output(tmp_path, stage, category):
    journal = SessionJournal(tmp_path / 'case.jsonl', session_id='case')
    now = datetime.now(timezone.utc)
    request = journal.append('runtime_event', {'eventType': 'provider.model.request',
        'timestamp': (now - timedelta(days=10)).isoformat(),
        'payload': {'callId': 'expired-call', 'model': 'm', 'stage': stage, 'request': {'media': 'expired-secret'}}})
    provider(journal, 'response', callId='expired-call', model='m', stage=stage, output='later-secret', status='completed')
    provider(journal, 'request', callId='other-call', model='m', stage=stage, request={'text': 'keep'})
    assert detail(journal, request['id'])['outputs']
    erase_expired(journal, {category: now - timedelta(days=1)})
    raw = journal.path.read_text(encoding='utf8')
    assert 'expired-secret' not in raw and 'later-secret' not in raw and 'keep' in raw
    with pytest.raises(ValueError): detail(journal, request['id'])


@pytest.mark.parametrize('kind,category', [('scene.request', 'observation'), ('scene.transcription.request', 'transcript'), ('long_event.request', 'memory')])
def test_exact_bodies_obey_source_content_retention(tmp_path, kind, category):
    journal = SessionJournal(tmp_path / 'case.jsonl', session_id='case')
    request = journal.append(kind, {'model': 'm'})
    record_request(journal, request, {'raw': 'private-input'})
    record_output(journal, request, {'raw': 'private-output'})
    erase_expired(journal, {category: datetime.now(timezone.utc) + timedelta(seconds=1)})
    raw = journal.path.read_text(encoding='utf8')
    assert 'private-input' not in raw and 'private-output' not in raw


def test_model_audit_api_is_owner_and_session_scoped(tmp_path):
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path/'runtime.json'), 'test', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/api/prompts?modelCalls=1')).status == 401
            uid = (await (await client.post('/api/auth/register',json={'username':'audit-owner','password':'valid-password-123'})).json())['user']['id']
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            journal = SessionJournal(tmp_path/'users'/uid/'sessions'/(sid+'.jsonl'), session_id=sid)
            row = provider(journal, 'request', callId='test',model='m',stage='asr',request={'audio':'private-context'})
            response = await client.get('/api/prompts', params={'session':sid,'modelCalls':'1'})
            assert response.headers['Cache-Control'] == 'private, no-store'
            assert 'private-context' not in await response.text()
            assert (await (await client.get('/api/prompts',params={'session':sid,'modelCall':row['id']})).json())['call']['inputs'][0]['content']['audio']=='private-context'
            other = (await (await client.post('/api/sessions')).json())['session']['id']
            assert (await client.get('/api/prompts',params={'session':other,'modelCall':row['id']})).status == 400
            await client.post('/api/auth/logout')
            await client.post('/api/auth/register',json={'username':'audit-other','password':'valid-password-123'})
            assert (await client.get('/api/prompts',params={'session':sid,'modelCalls':'1'})).status in {400,403,404}
    asyncio.run(run())
