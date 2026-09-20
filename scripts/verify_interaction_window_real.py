"""Replay real saved camera samples through product WebSocket, models and TTS.

The camera samples are authentic; their replay clock and test questions are
controlled fixtures, not a fresh physical camera/microphone measurement.
"""
import argparse
import asyncio
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


async def verify(source, output, *, extended=False):
    output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines()]
    observations = [r for r in rows if r['type'] == 'ambient_observation' and
                    '2026-09-18T04:48:10' <= (r.get('evidence') or {}).get('capturedAt', '') <= '2026-09-18T04:48:19']
    assert len(observations) == 2, 'Expected frozen hand-motion and subsequent resting camera samples'
    report = {'passed': False, 'realModels': True, 'realSavedCameraSamples': True,
              'physicalLiveCameraTest': False, 'inputChannel': 'explicit-control',
              'originalSources': observations, 'turns': [], 'semanticReview': 'manual-required',
              'passCriteria': 'product chain, audio delivery and valid visual citations; not semantic accuracy'}
    questions = ['刚才画面中那个人的手臂有什么动作？和最后一帧有什么不同？请简短回答。',
                 '那现在他的手还在刚才的位置吗？只说画面能确认的。']
    if extended:
        questions += ['那最后能确定他把手放在哪里了吗？看不见就直说。',
                      '顺便帮我看一下，他穿的鞋是什么颜色？']
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / 'runtime.json'), 'qwen3.5-omni-flash-realtime', 'Ethan')
    try:
        async with TestClient(TestServer(app)) as client:
            assert (await client.post('/api/auth/register', json={'username': 'interaction-check', 'password': 'interaction-av-check-123'})).status == 200
            sid = (await (await client.post('/api/sessions', json={'label': 'Recent audiovisual context replay'})).json())['session']['id']
            report['sessionId'] = sid
            async with client.ws_connect('/ws/realtime?session=' + sid) as ws:
                async with asyncio.timeout(60):
                    while (await ws.receive_json()).get('type') != 'ready':
                        pass
                origin = datetime.fromisoformat(observations[0]['evidence']['capturedAt'])
                latest = 0
                for row in observations:
                    e = row['evidence']; ref = e['media']
                    latest = 10000 + (datetime.fromisoformat(e['capturedAt']) - origin).total_seconds() * 1000
                    data = (source.parent / 'media' / source.stem / ref['assetId']).read_bytes()
                    await ws.send_json({'type': 'video', 'image': base64.b64encode(data).decode(),
                                       'capture': {'clockId': 'recorded-replay', 'monotonicMs': latest,
                                                   'originalCapturedAt': e['capturedAt']}})
                for question in questions:
                    tid = uuid4().hex; started = time.perf_counter(); pcm = bytearray()
                    record = {'turnId': tid, 'question': question, 'events': []};report['turns'].append(record)
                    await ws.send_json({'type': 'control', 'clientRequestId': tid, 'transcript': question,
                                       'capture': {'clockId': 'recorded-replay', 'monotonicMs': latest + 50}})
                    async with asyncio.timeout(70):
                        while True:
                            event = await ws.receive_json();kind = event.get('type')
                            if kind == 'state':continue
                            elapsed = round((time.perf_counter() - started) * 1000, 1)
                            if event.get('audio'):
                                audio = base64.b64decode(event.pop('audio'));pcm.extend(audio)
                                event['audioBytes'] = len(audio);record.setdefault('firstAudioMs', elapsed)
                            if kind == 'assistant_transcript' and event.get('origin', {}).get('turnId') == tid:
                                record.setdefault('firstTextMs', elapsed)
                                record['answer'] = event['text'] if event.get('done') else record.get('answer', '') + event.get('text', '')
                            record['events'].append({'elapsedMs': elapsed, **event})
                            assert kind not in {'error', 'provider_unavailable'}, event
                            if kind == 'response_status' and event.get('origin', {}).get('turnId') == tid and event.get('status') in {'completed', 'interrupted'}:
                                assert event.get('status') == 'completed', event
                                break
                    assert pcm and record.get('answer')
                    with wave.open(str(output / (tid + '.wav')), 'wb') as wav:
                        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(pcm)
                    state = await (await client.get('/api/state', params={'session': sid})).json()
                    turn = next(r for r in state['sharedScene']['utterances'] if r['turnId'] == tid)
                    record['window'] = turn['interactionWindow']
                    assert len(record['window']['frames']) == 2
                    print(json.dumps({k:v for k,v in record.items() if k not in {'events','window'}}, ensure_ascii=True), flush=True)
            journal = next(output.glob(f'users/*/sessions/{sid}.jsonl'))
            records = [json.loads(s) for s in journal.read_text(encoding='utf8').splitlines()]
            report['visualResults'] = [r for r in records if r['type'] == 'scene.question.result']
            assert len(report['visualResults']) == len(questions)
            assert all(r['status'] == 'observed' and r['scope'] == 'interaction-sequence'
                       and (r.get('evidence') or r.get('uncertainties')) for r in report['visualResults'])
            report['passed'] = True
    finally:
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--extended', action='store_true')
    args = parser.parse_args();load_env()
    raise SystemExit(0 if asyncio.run(verify(args.journal, args.output, extended=args.extended)) else 1)
