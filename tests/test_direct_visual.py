import asyncio
import json
from pathlib import Path
import subprocess

import pytest

from egoassist.realtime.direct_visual import prepared_visual_answer, validate_direct_answer
from test_modular_realtime import conversation, settle
from egoassist.product.answer_jobs import AnswerJobs, answer_jobs
from egoassist.core.journal import SessionJournal


ANSWER = {'text': '画面中是红白格桌布。', 'model': 'test-vl', 'sourceIds': ['frame-1']}
SCENE = {'visual': {'status': 'observed', 'model': 'test-vl', 'directAnswer': ANSWER}}


@pytest.mark.parametrize('extra', [
    {'memoryQuery': '今天喝了几次水'}, {'memoryQuerySpec': {'scope': 'today'}},
    {'memoryText': '请记住'}, {'taskControl': 'start'}, {'selectedSkill': 'cooking'},
    {'interaction': {'action': 'listen'}}, {'intents': [{'kind': 'background-task'}]},
    {'memoryEvents': [{'text': '我喜欢红色'}]}, {'status': 'invalid'},
])
def test_complex_requests_cannot_reuse_direct_visual(extra):
    assert prepared_visual_answer(SCENE, {'status': 'classified', **extra}) is None


def test_validated_sources_and_standalone_scope():
    valid = {'scope': 'visible-only', 'text': '红白格', 'sourceIds': ['F01']}
    evidence = [{'description': '红白格', 'sourceIds': ['F01']}]
    assert validate_direct_answer({'directAnswer': valid, 'evidence': evidence}, {'F01': 'real'})['sourceIds'] == ['real']
    assert validate_direct_answer({'directAnswer': valid, 'evidence': []}, {'F01': 'real'}) is None
    assert validate_direct_answer({'directAnswer': valid}, {'F02': 'other'}) is None
    assert validate_direct_answer({'directAnswer': {**valid, 'scope': 'today'}}, {'F01': 'real'}) is None
    assert prepared_visual_answer(SCENE, {'status': 'classified'})['model'] == 'test-vl'
    assert prepared_visual_answer(SCENE, {'status': 'classified'}, task=True) is None
    assert prepared_visual_answer(SCENE, {'status': 'classified'}, compound=True) is None


@pytest.mark.parametrize('mode', ['speech', 'text', 'tts-failure', 'cancel'])
def test_prepared_visual_never_calls_text_model(mode):
    async def run():
        c, callback = conversation(text_only=True)
        if mode == 'tts-failure':
            async def fail(*args):
                raise ConnectionError('offline')
            c.provider.speak_stream = fail
        c.create_response(instructions='ignored for prepared answer',
                          output_modalities=['text'] if mode == 'text' else ['audio', 'text'],
                          prepared_answer=ANSWER)
        if mode == 'cancel':
            c.cancel_response()
        await settle(lambda: any(e['type'] in {'response.done', 'response.cancelled'} for e in callback.events))
        assert c.provider.messages == [] and c.provider.frames == []
        terminal = [e for e in callback.events if e['type'] in {'response.done', 'response.cancelled'}][-1]
        assert terminal['response']['model'] == 'test-vl'
        if mode == 'cancel':
            assert terminal['type'] == 'response.cancelled'
        else:
            assert any(e.get('text', e.get('transcript')) == ANSWER['text'] for e in callback.events)
        if mode == 'tts-failure':
            assert terminal['response']['audioAvailable'] is False
        await c.aclose()
    asyncio.run(run())


def test_interrupted_answer_recovers_without_regeneration(tmp_path):
    async def run():
        async def forbidden(*args):
            pytest.fail('must reuse the existing visual answer')
        journal = SessionJournal(tmp_path / 'case.jsonl')
        manager = AnswerJobs('test', generate=forbidden)
        manager.register('user', journal, turn_id='turn', question='桌布什么颜色',
                         instructions='evidence', prepared_result=ANSWER)
        manager.interrupt(journal, 'turn')
        await manager._execute(journal, answer_jobs(journal)['turn'])
        row = answer_jobs(journal)['turn']
        assert row['status'] == 'completed' and row['text'] == ANSWER['text']
        assert row['model'] == 'test-vl' and row['attempt'] == 0
        await manager.close()
    asyncio.run(run())


def test_ui_reports_actual_stages_and_correlates_tts():
    source = Path('egoassist/console/model_stages.js').read_text(encoding='utf8')
    events = [
        {'type': 'response.requested', 'model': 'unused-omni', 'payload': {'turnId': 't'}},
        {'type': 'response.created', 'model': 'unused-omni', 'responseId': 'r',
         'payload': {'model': 'test-vl', 'origin': {'turnId': 't'}}},
        {'type': 'response.dispatched', 'payload': {'origin': {'turnId': 't'}, 'backend': 'visual-answer-and-tts', 'model': 'test-vl'}},
        {'type': 'provider.pipeline.metrics', 'payload': {'responseId': 'r', 'stage': 'tts', 'apiCalls': 1, 'model': 'tts-model'}},
        {'type': 'provider.pipeline.metrics', 'payload': {'responseId': 'r', 'stage': 'response', 'apiCalls': 0, 'model': 'unused-omni'}},
        {'type': 'provider.pipeline.metrics', 'payload': {'responseId': 'other', 'stage': 'text', 'apiCalls': 1, 'model': 'other-model'}},
    ]
    script = source + '\nconsole.log(JSON.stringify(turnModelStages({turnId:"t",semantic:{model:"jev"}},' + json.dumps(events) + ')));'
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, encoding='utf8', check=True)
    assert [s['model'] for s in json.loads(result.stdout)] == ['jev', 'test-vl', 'tts-model']
