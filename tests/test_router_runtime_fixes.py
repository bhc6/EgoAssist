import asyncio
from datetime import datetime, timedelta, timezone
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from egoassist.core.journal import SessionJournal
from egoassist.realtime.interaction import InteractionState
from egoassist.realtime.modular import SpeechSegments
from egoassist.realtime.scene import SharedScene
from egoassist.realtime.decisions_router import DecisionsTurnRouter
from test_decisions_router import typed_body


def test_old_playback_is_not_live_after_restart_or_reconnect(tmp_path):
    journal = SessionJournal(tmp_path/'session.jsonl', session_id='session')
    saved = journal.append('interaction.playback', {'responseId':'old', 'status':'playing'})
    state = InteractionState(journal)
    assert state.playback_context() is None
    state.responses['new'] = {'origin':{}, 'pcm':bytearray(20), 'playedSamples':0, 'text':'hello', 'complete':False}
    state.selected_response = 'new'
    assert state.playback_context()['responseId'] == 'new'
    state.reset_playback()
    assert state.playback_context() is None
    assert journal.entries_by_id([saved['id']])[saved['id']]['status'] == 'playing'


def test_live_observation_window_excludes_stale_but_keeps_history(tmp_path):
    journal = SessionJournal(tmp_path/'session.jsonl', session_id='session')
    scene = SharedScene(journal, '')
    now = datetime.now(timezone.utc)
    for name, delta in [('yesterday', -86400), ('recent', -2), ('future', 5)]:
        scene.ingest_observation(journal.append('ambient_observation', {'status':'accepted', 'evidence':{
            'capturedAt':(now+timedelta(seconds=delta)).isoformat(), 'visibleEvidence':[name]}}))
    assert scene.observation_context(now.isoformat())['visibleEvidence'] == ['recent']
    assert scene.observation_context((now+timedelta(seconds=30)).isoformat()) is None
    assert len(journal.entries(types={'ambient_observation'})) == 3


def test_vad_ignores_impulse_but_preserves_sustained_speech_onset():
    class Vad:
        def is_speech(self, frame, rate): return any(frame)
    started, ended = [], []
    segments = SpeechSegments(lambda *args: started.append(args), lambda *args: ended.append(args), vad=Vad())
    voice, silence = b'\x01\x00'*320, bytes(640)
    segments.feed(voice*3+silence*40)
    assert not started
    segments.feed(voice*12+silence*33)
    assert len(started) == len(ended) == 1
    assert voice*12 in ended[0][2]


def test_review_and_main_request_share_deadline():
    async def run():
        router = DecisionsTurnRouter('qwen', decisions_key='test', timeout_seconds=.12, decisions_timeout=.1)
        router.set_turn_context(clock_context=None, memory_draft=None,
            active_task={'currentStep':{'completion':'ready'}})
        async def decide(payload, audit):
            await asyncio.sleep(.08)
            return typed_body(payload['questions'], route='task', taskControl='confirm-step', verdict='confirm-step')['answers']
        router._request_decisions = decide
        start = time.perf_counter()
        result = await router.classify('ready', [])
        assert result['status'] == 'unavailable' and result['timedOut']
        assert result['taskControl'] is None
        assert time.perf_counter()-start < .22
    asyncio.run(run())


def test_late_result_is_not_accepted_when_timeout_callback_is_delayed():
    async def run():
        router = DecisionsTurnRouter('qwen', decisions_key='test', timeout_seconds=.03, decisions_timeout=.02)
        async def decide(payload, audit):
            time.sleep(.06)  # Simulate a blocking callback, not a slow provider.
            return typed_body(payload['questions'], route='reply')['answers']
        router._request_decisions = decide
        result = await router.classify('hello', [])
        assert result['status'] == 'unavailable' and result['timedOut']
        assert result['requestMetrics']['deadlineOverrunMs'] > 0
    asyncio.run(run())


def test_timeout_audit_records_attempt_and_error(monkeypatch):
    async def run():
        release = asyncio.Event()
        async def handle(request):
            await release.wait()
            return web.json_response({})
        remote = web.Application(); remote.router.add_post('/', handle)
        async with TestServer(remote) as server:
            monkeypatch.setattr('egoassist.realtime.decisions_router.DECISIONS_URL', str(server.make_url('/')))
            router = DecisionsTurnRouter('qwen', decisions_key='test', timeout_seconds=.12, decisions_timeout=.06)
            try:
                result = await router.classify('hello', [])
                audit = result['requestMetrics']['decisions']
                assert audit['attempts'] == len(audit['attemptDetails']) == 1
                assert audit['attemptDetails'][0]['error'] and audit['error']
                assert audit['attemptDetails'][0]['latencyMs'] > 0
            finally:
                release.set(); await router.aclose()
    asyncio.run(run())


def test_insight_poll_never_reads_whole_journal(tmp_path, monkeypatch):
    from egoassist import BUILTIN_SKILLS_ROOT
    from run_egoassist import create_app
    async def run():
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(tmp_path/'runtime.json'), 'test', 'Ethan')
        async with TestClient(TestServer(app)) as client:
            await client.post('/api/auth/register', json={'username':'poll-user','password':'test-password-123'})
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            # Initialize the owner before guarding reads from the polling endpoint.
            await client.get('/api/state', params={'session':sid})
            original = SessionJournal.entries
            def bounded(self, *, types=None, tail=None):
                assert types is not None or tail is not None, 'poll copied the full journal'
                return original(self, types=types, tail=tail)
            monkeypatch.setattr(SessionJournal, 'entries', bounded)
            res = await client.get('/api/insights', params={'session':sid,'includeResults':'false'})
            assert res.status == 200, await res.text()
            assert (await res.json())['results'] == []
    asyncio.run(run())
