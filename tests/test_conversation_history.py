import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.product.conversation_history import conversation_page
from egoassist.realtime.work_ledger import CaseWorkLedger
from run_egoassist import create_app


def test_history_speaker_identity_updates_after_confirmation_merge_and_undo(tmp_path):
    from egoassist.realtime.scene import SharedScene
    journal = SessionJournal(tmp_path/'speakers.jsonl')
    ledger = CaseWorkLedger(journal)
    for sid, vector in [('s1', [1., 0.]), ('s2', [0., 1.])]:
        journal.append('speaker.profile', {'speakerId': sid, 'embedding': vector,
            'wearer': False, 'name': None, 'identitySource': 'unknown'})
    for i, sid in enumerate(('s1', 's2', None)):
        ledger.capture_turn('你好', source='input-audio-transcript', turn_id='t'+str(i))
        journal.append('scene.turn', {'turnId': 't'+str(i), 'speaker': {
            'status': 'single-speaker' if sid else 'unknown', 'speakerId': sid, 'wearer': False}})
    ledger.capture_turn('文字', source='explicit-control', turn_id='typed')
    scene = SharedScene(journal, '')
    def labels():
        return [r['speakerAttribution']['kind'] for r in conversation_page(journal, scene=scene)['messages']]
    assert labels() == ['unknown', 'unknown', 'unknown', 'account']
    scene.speakers.confirm('s1', name='我', wearer=True)
    scene.speakers.confirm('s2', name='同事', wearer=False)
    assert labels() == ['self', 'other', 'unknown', 'account']
    scene.speakers.merge('s2', 's1')
    assert labels() == ['self', 'self', 'unknown', 'account']
    scene.speakers.undo_merge(scene.speakers.merges[-1]['id'])
    assert labels() == ['self', 'other', 'unknown', 'account']
    restored = SharedScene(SessionJournal(journal.path), '')
    assert [r['speakerAttribution']['kind'] for r in conversation_page(restored.journal, scene=restored)['messages']] == labels()


def test_mixed_speech_history_retains_unknown_overlap_and_per_segment_attribution(tmp_path):
    from egoassist.realtime.scene import SharedScene
    journal = SessionJournal(tmp_path/'mixed.jsonl')
    journal.append('speaker.profile', {'speakerId': 'mine', 'embedding': [1., 0.], 'wearer': True,
        'name': '我', 'identitySource': 'account-confirmed'})
    CaseWorkLedger(journal).capture_turn('混合转写', source='input-audio-transcript', turn_id='mixed')
    segments = [{'startMs': 0, 'endMs': 1200, 'speakerId': 'mine', 'cluster': 0},
        {'startMs': 1200, 'endMs': 2000, 'speakerId': None, 'cluster': 1, 'method': 'overlapping-speech'}]
    journal.append('scene.turn', {'turnId': 'mixed', 'speaker': {'status': 'mixed-or-unknown', 'segments': segments},
        'utterances': [{**segments[0], 'text': '我的话', 'status': 'transcribed'},
                      {**segments[1], 'text': None, 'status': 'overlapping-speech'}]})
    scene = SharedScene(journal, '')
    speaker = conversation_page(journal, scene=scene)['messages'][0]['speakerAttribution']
    assert speaker['kind'] == 'mixed'
    assert [s['kind'] for s in speaker['segments']] == ['self', 'unknown']
    assert speaker['segments'][0]['text'] == '我的话'
    assert speaker['segments'][1]['label'] == '无法确认 · 同时说话'


def test_voice_receipt_uses_persisted_turn_identity_before_routing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from egoassist.realtime.omni import OmniBrowserBridge
    from egoassist.realtime.turn_coordinator import TurnCoordinator
    monkeypatch.setenv('EGOASSIST_REALTIME_BACKEND', 'omni')

    async def run():
        journal = SessionJournal(tmp_path/'voice.jsonl')
        ledger = CaseWorkLedger(journal)
        coordinator = TurnCoordinator.__new__(TurnCoordinator)
        coordinator.owner = SimpleNamespace(turn_sequence=0, latest_input_sequence=0,
            output_generation=0, work_ledger=ledger, scene=SimpleNamespace(capture=lambda *a,**k:None))
        coordinator.harness = SimpleNamespace(journal=journal)
        coordinator.interaction_state = SimpleNamespace(pause=lambda:None)
        coordinator.mark_interrupted_turns = lambda _ledger:None
        queued = []
        coordinator.enqueue_persisted_turn = queued.append
        emitted = []
        async def emit(event):
            if event['type']=='user_transcript':
                saved = conversation_page(journal)['messages']
                assert any(row['id']=='user:'+event['turnId'] for row in saved)
                emitted.append(event)
        bridge = OmniBrowserBridge('unused','unused','Ethan','test',emit,lambda _:None)
        bridge.on_transcript_evidence = lambda text,segment: coordinator._do_enqueue_transcript(text,segment=segment)
        bridge.handle_user_speech_started = lambda **kwargs:None
        bridge._input_segment_transcribed = lambda *args:{}
        callback = bridge._Callback(bridge._dialogue_generation)
        # Identical spoken words are distinct turns, not text-based duplicates.
        for _ in range(2):
            callback.on_event({'type':'conversation.item.input_audio_transcription.completed','transcript':'你好'})
        async with asyncio.timeout(2):
            while len(emitted)<2:
                await asyncio.sleep(.001)
        assert len(queued)==2 and len({e['turnId'] for e in emitted})==2
        assert all(e['persisted'] and e['timestamp'] and e['text']=='你好' for e in emitted)
        assert [e['turnId'] for e in emitted]==[r['turnId'] for r in queued]
        assert conversation_page(SessionJournal(journal.path))['total']==2
        await bridge.close()
    asyncio.run(run())


def test_history_keeps_all_turns_partial_and_standalone_answers_across_restart(tmp_path):
    path = tmp_path / 'history.jsonl'
    journal = SessionJournal(path)
    ledger = CaseWorkLedger(journal)
    for i in range(125):
        ledger.capture_turn('Question ' + str(i), source='input-audio-transcript', sequence=i, turn_id='t'+str(i))
    journal.append('runtime_event', {'eventType': 'response.created', 'responseId': 'r', 'payload': {'origin': {'turnId': 't0'}}})
    journal.append_message('assistant', 'An interrupted sentence', status='interrupted', source='provider-output', response_id='r')
    journal.append_message('assistant', 'Saved your preference', source='server-memory-policy', response_id='receipt')
    job = {'turnId': 't0', 'status': 'completed', 'channel': 'background-text', 'text': 'Finished later'}
    journal.append('answer.job', job)
    journal.append('answer.job', job)  # State repeat must not duplicate the answer.
    restored = SessionJournal(path)
    page = conversation_page(restored, limit=30)
    all_rows = page['messages']
    while page['nextBefore']:
        page = conversation_page(restored, before=page['nextBefore'], limit=30)
        all_rows = page['messages'] + all_rows
    assert len(all_rows) == 128
    assert len({r['id'] for r in all_rows}) == 128
    assert [r['text'] for r in all_rows[:125]] == ['Question ' + str(i) for i in range(125)]
    assert all_rows[-3]['status'] == 'interrupted' and all_rows[-3]['turnId'] == 't0'
    assert all_rows[-2]['text'] == 'Saved your preference'
    assert all_rows[-1]['source'] == 'background-text'


def test_history_is_read_only_untruncated_and_erasure_invalidates_cache(tmp_path):
    journal = SessionJournal(tmp_path / 'history.jsonl')
    text = '<script>untrusted</script>\n' + 'long conversation ' * 1000
    row = journal.append_message('assistant', text, source='provider-output', response_id='r')
    leaf = journal.leaf_id
    assert conversation_page(journal)['messages'][0]['text'] == text.strip()
    assert journal.leaf_id == leaf
    journal.rewrite(lambda entries: [r for r in entries if r['id'] != row['id']])
    assert conversation_page(journal)['messages'] == []
    with pytest.raises(ValueError):
        conversation_page(journal, before='assistant:r')
    with pytest.raises(ValueError):
        conversation_page(journal, limit=201)


def test_history_endpoint_checks_account_session_and_paginates(tmp_path):
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path/'runtime.json'), 'test', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/api/conversation')).status == 401
            user = (await (await client.post('/api/auth/register', json={'username': 'history-owner', 'password': 'valid-password-123'})).json())['user']
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            # Create genuine journal records without invoking any provider or changing user state.
            journal = SessionJournal(tmp_path/'users'/user['id']/'sessions'/(sid+'.jsonl'), session_id=sid)
            for i in range(105):
                journal.append_message('user', 'message '+str(i), source='fixture')
            page = await (await client.get('/api/conversation', params={'session': sid, 'limit': 50})).json()
            assert page['total'] == 105 and len(page['messages']) == 50
            assert page['messages'][0]['text'] == 'message 55'
            assert (await client.get('/api/conversation', params={'session': sid, 'limit': 'wrong'})).status == 400
            await client.post('/api/auth/register', json={'username': 'history-other', 'password': 'valid-password-456'})
            assert (await client.get('/api/conversation', params={'session': sid})).status in {400, 403, 404}
    asyncio.run(run())
