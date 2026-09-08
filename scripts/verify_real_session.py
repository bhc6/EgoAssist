"""Real provider acceptance via the production app, with isolated user data.

No mocks: requires QWEN_API_KEY. Exits nonzero without a connected provider,
completed transcript and audio. Text input does not measure microphone/ASR.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app


async def verify(output, model, scenario='greeting'):
    load_env(str(ROOT / '.env'))
    output.mkdir(parents=True, exist_ok=False)
    report = {'model': model, 'mock': False, 'input': 'text-control', 'passed': False,
              'microphoneAsrMeasured': False, 'events': [], 'audioPackets': 0}
    turns = (['记住：我的储物柜编号是7429。', '我的储物柜编号是什么？']
             if scenario == 'memory' else ['你好，请用一句话介绍你自己。'])
    report['scenario'] = scenario
    report['turns'] = []
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / 'runtime.json'), model, 'Ethan')
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/api/auth/register', json={
                'username': 'real-acceptance', 'password': 'isolated-test-account-123'})
            assert response.status == 200
            response = await client.post('/api/sessions')
            sid = (await response.json())['session']['id']
            report['sessionId'] = sid
            started = time.perf_counter()
            async with client.ws_connect('/ws/realtime?session=' + sid) as ws:
                sent = None
                async with asyncio.timeout(60):
                    while True:
                        if report.get('transcript') and report['audioPackets']:
                            journal_path = next(output.glob('users/*/sessions/' + sid + '.jsonl'))
                            rows = [json.loads(line) for line in journal_path.read_text(encoding='utf-8').splitlines() if line.strip()]
                            terminal = [row for row in rows if row.get('eventType') == 'response.finished']
                            provider_turns = sum(t.get('channel') != 'server-memory-policy' for t in report['turns'])
                            if len(terminal) > provider_turns:
                                detail = terminal[-1]['payload']
                                report['responseFinished'] = detail
                                report['passed'] = not detail.get('interrupted') and detail.get('status') == 'response.done'
                                report['finishedMs'] = round((time.perf_counter()-sent)*1000, 1)
                                report['providerLatencyMs'] = {r['eventType']: r.get('latencyMs') for r in rows if r.get('eventType') in {'response.first_audio', 'response.first_transcript'}}
                                report['routingLatencyMs'] = [r['decision'].get('latencyMs') for r in rows if r.get('type') == 'control_decision']
                                report['routingStatuses'] = [r['decision'].get('status') for r in rows if r.get('type') == 'control_decision']
                                report['turns'].append({k: report.get(k) for k in (
                                    'passed', 'transcript', 'firstTextMs', 'firstAudioMs', 'finishedMs', 'audioPackets')})
                                if not report['passed']:
                                    break
                                if len(report['turns']) < len(turns):
                                    for key in ('transcript', 'firstTextMs', 'firstAudioMs', 'transcriptDoneMs'):
                                        report.pop(key, None)
                                    report['audioPackets'] = 0
                                    sent = time.perf_counter()
                                    await ws.send_json({'type': 'control', 'transcript': turns[len(report['turns'])]})
                                    continue
                                if scenario == 'memory':
                                    report['memoryAnswerCorrect'] = '7429' in report['transcript']
                                    committed = set(report['memoryReceipt']['committedCardIds'])
                                    report['memoryRetrieved'] = any(
                                        candidate.get('selected') and candidate.get('episodeId') in committed
                                        for row in rows if row.get('eventType') == 'episodic.memory.retrieved'
                                        for candidate in row.get('payload', {}).get('candidates', [])
                                    )
                                    report['passed'] = report['passed'] and report['memoryAnswerCorrect'] and report['memoryRetrieved']
                                break
                        try:
                            message = await ws.receive(timeout=0.2)
                        except asyncio.TimeoutError:
                            continue
                        if message.type != WSMsgType.TEXT:
                            raise RuntimeError('WebSocket closed without completed response')
                        payload = json.loads(message.data)
                        kind = payload.get('type')
                        now = time.perf_counter()
                        report['events'].append({'type': kind, 'elapsedMs': round((now-started)*1000, 1)})
                        if kind in {'error', 'provider_unavailable'}:
                            raise RuntimeError(str(payload.get('message') or payload.get('reason'))[:400])
                        if kind == 'memory_status' and scenario == 'memory' and not report['turns']:
                            if payload.get('status') != 'committed' or not payload.get('committedCardIds'):
                                raise RuntimeError('Memory was not committed')
                            if '7429' not in payload.get('message', '') or '电话' in payload.get('message', ''):
                                raise RuntimeError('Memory receipt corrupted the original fact')
                            report['memoryReceipt'] = payload
                            report['turns'].append({'passed': True, 'channel': 'server-memory-policy',
                                                   'finishedMs': round((now-sent)*1000, 1),
                                                   'audioPackets': 0})
                            sent = time.perf_counter()
                            await ws.send_json({'type': 'control', 'transcript': turns[1]})
                        if kind == 'ready' and sent is None:
                            report['lanes'] = payload.get('lanes')
                            if not (payload.get('lanes') or {}).get('dialogue', {}).get('connected'):
                                raise RuntimeError('Provider is not connected despite ready event')
                            report['connectMs'] = round((now-started)*1000, 1)
                            sent = time.perf_counter()
                            await ws.send_json({'type': 'control', 'transcript': turns[0]})
                        if sent is not None and kind == 'audio' and payload.get('audio'):
                            report.setdefault('firstAudioMs', round((now-sent)*1000, 1))
                            report['audioPackets'] += 1
                        if sent is not None and kind == 'assistant_transcript':
                            report.setdefault('firstTextMs', round((now-sent)*1000, 1))
                            if payload.get('done') and payload.get('text'):
                                report['transcript'] = payload['text']
                                report['transcriptDoneMs'] = round((now-sent)*1000, 1)
    except Exception as exc:
        report['passed'] = False
        report['error'] = f'{type(exc).__name__}: {exc}'
    (output / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='qwen3.5-omni-flash-realtime')
    parser.add_argument('--scenario', choices=('greeting', 'memory'), default='greeting')
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(verify(args.output, args.model, args.scenario)) else 1)
