"""Real provider latency with a copied long journal; no old actions are replayed."""
import argparse
import asyncio
import base64
import cProfile
import hashlib
import io
import json
import os
from pathlib import Path
import pstats
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.core.journal import SessionJournal
from egoassist.core.events import latest
from egoassist.realtime.work_ledger import CaseWorkLedger
from run_egoassist import create_app
from verify_real_voice_stop import synthesize


async def verify(args):
    load_env()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'scope': 'Real models with isolated copied long history; TTS audio input, no physical microphone or continuous live camera.',
              'sourceSha256': hashlib.sha256(args.source.read_bytes()).hexdigest(), 'turns': [],
              'routerBackend': os.getenv('EGOASSIST_TURN_ROUTER_BACKEND', 'qwen'),
              'budgetsMs': {'receipt': args.receipt_budget_ms, 'firstAudio': args.first_audio_budget_ms}}
    root = Path(__file__).resolve().parents[1]
    source_paths = [Path(__file__), *(root / 'egoassist').rglob('*.py'), *(root / 'egoassist/prompts').glob('*.json')]
    plan = {'sourceSha256': report['sourceSha256'], 'voice': args.voice, 'burst': args.burst,
            'budgetsMs': report['budgetsMs'], 'routerBackend': report['routerBackend'],
            'hashes': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}}
    (args.output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    runtime = args.output / 'runtime'
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(runtime / 'memory.json'),
                     'qwen3.5-omni-flash-realtime', 'Ethan')
    profile = cProfile.Profile()
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/api/auth/register', json={'username':'latency-replay','password':'isolated-latency-20260924'})
            assert response.status == 200
            uid = (await response.json())['user']['id']
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            report['sessionId'] = sid
            lines = args.source.read_text(encoding='utf-8').splitlines()
            header = json.loads(lines[0]); header['id'] = sid; header.setdefault('metadata', {})['userId'] = uid
            lines[0] = json.dumps(header, ensure_ascii=False)
            path = runtime / 'users' / uid / 'sessions' / (sid + '.jsonl')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            journal = SessionJournal(path)
            ledger = CaseWorkLedger(journal)
            ledger.cancel_pending_turns(reason='isolated-latency-replay-no-old-actions')
            for kind, key in [('answer.job','turnId'), ('memory.recovery.job','turnId'), ('insight.job','jobId')]:
                for row in latest(journal, kind, key).values():
                    if row.get('status') not in {'completed','cancelled','failed'}:
                        journal.append(kind, {k:v for k,v in {**row,'status':'cancelled'}.items() if k not in {'id','parentId','timestamp','type'}})
            report['sourceRows'] = len(lines)
            profile.enable()
            started = time.perf_counter()
            async with client.ws_connect('/ws/realtime?session='+sid) as ws:
                async with asyncio.timeout(60):
                    while (await ws.receive_json()).get('type') != 'ready': pass
                report['connectionReadyMs'] = round((time.perf_counter()-started)*1000,1)
                profile.disable()
                timings = []
                for _ in range(3):
                    started = time.perf_counter()
                    await (await client.get('/api/state',params={'session':sid})).read()
                    timings.append(round((time.perf_counter()-started)*1000,1))
                report['stateReadMs'] = timings
                cases = [('text','请用阿拉伯数字简短回答：二加三等于多少？','5'),
                         ('text','把你刚才算出的结果乘以四，等于多少？请用阿拉伯数字简短回答。','20')]
                if args.voice: cases.append(('audio','助手，请用阿拉伯数字简短回答，七乘八等于多少？','56'))
                for channel, question, expected in cases:
                    tid = uuid4().hex if channel == 'text' else None
                    row = {'channel':channel, 'question':question, 'expected':expected, 'turnId':tid, 'events':[]}
                    report['turns'].append(row)
                    pcm = None
                    if channel == 'audio': pcm, row['fixture'] = await synthesize(question, args.output, 0)
                    started = time.perf_counter()
                    if channel == 'text':
                        await ws.send_json({'type':'control','clientRequestId':tid,'transcript':question})
                    async def feed():
                        for offset in range(0,len(pcm),3200):
                            await ws.send_json({'type':'audio','audio':base64.b64encode(pcm[offset:offset+3200]).decode()})
                            await asyncio.sleep(0.1)
                        row['audioInputEndMs'] = round((time.perf_counter()-started)*1000,1)
                        for _ in range(20):
                            await ws.send_json({'type':'audio','audio':base64.b64encode(bytes(3200)).decode()})
                            await asyncio.sleep(0.1)
                    feed_task = asyncio.create_task(feed()) if pcm else None
                    try:
                        async with asyncio.timeout(55):
                            while True:
                                event = await ws.receive_json()
                                elapsed = round((time.perf_counter()-started)*1000,1)
                                kind = event.get('type')
                                if kind == 'state': continue
                                if 'audio' in event:
                                    event['audioBytes'] = len(base64.b64decode(event.pop('audio')))
                                row['events'].append({'elapsedMs':elapsed,**event})
                                if kind == 'turn_receipt' and event.get('turnId') == tid: row['receiptMs'] = elapsed
                                if kind in {'error','provider_unavailable'}: raise RuntimeError(str(event))
                                origin = event.get('origin') or {}
                                if channel == 'audio' and not tid and origin.get('turnId'):
                                    tid = origin['turnId']; row['turnId'] = tid
                                if origin.get('turnId') != tid or not tid: continue
                                if kind == 'audio': row.setdefault('firstAudioMs',elapsed)
                                if kind == 'assistant_transcript':
                                    row.setdefault('firstTextMs',elapsed)
                                    row['answer'] = event['text'] if event.get('done') else row.get('answer','') + event.get('text','')
                                if kind == 'response_status' and event.get('status') == 'completed': break
                    finally:
                        if feed_task:
                            if not feed_task.done(): feed_task.cancel()
                            await asyncio.gather(feed_task,return_exceptions=True)
                    row['durationMs'] = round((time.perf_counter()-started)*1000,1)
                    row['answerPassed'] = expected in row.get('answer','') and bool(row.get('firstAudioMs'))
                    row['firstAudioAfterInputMs'] = round(row.get('firstAudioMs', float('inf')) - row.get('audioInputEndMs', 0), 1)
                    row['latencyPassed'] = (row['firstAudioAfterInputMs'] <= args.first_audio_budget_ms
                        and (channel != 'text' or row.get('receiptMs', float('inf')) <= args.receipt_budget_ms))
                    row['passed'] = row['answerPassed'] and row['latencyPassed']
                    print(json.dumps({k:v for k,v in row.items() if k!='events'},ensure_ascii=True),flush=True)
                    assert row['passed'], 'wrong/missing answer or exceeded latency budget'
                if args.burst:
                    burst = {'turns': [], 'scope': 'Two rapid text inputs on the same long session, checking both durable answers.'}
                    report['burst'] = burst
                    start = time.perf_counter()
                    for question, expected in [('只回答数字：十一加十二等于多少？','23'),('只回答数字：十二加十三等于多少？','25')]:
                        tid = uuid4().hex
                        burst['turns'].append({'turnId':tid,'question':question,'expected':expected})
                        await ws.send_json({'type':'control','clientRequestId':tid,'transcript':question})
                    async def consume_burst():
                        while True:
                            event = await ws.receive_json()
                            if event.get('type') == 'error': raise RuntimeError(str(event))
                            for row in burst['turns']:
                                if event.get('type') == 'turn_receipt' and event.get('turnId') == row['turnId']:
                                    row['receiptMs'] = round((time.perf_counter()-start)*1000,1)
                    consumer = asyncio.create_task(consume_burst())
                    try:
                        async with asyncio.timeout(40):
                            while True:
                                if consumer.done(): consumer.result()
                                state = await (await client.get('/api/state',params={'session':sid})).json()
                                turns = {t['turnId']: t for t in state['workLedger']['turnInbox']}
                                for row in burst['turns']:
                                    answer = turns.get(row['turnId'],{}).get('answer') or {}
                                    if answer.get('status') == 'completed':
                                        row['answer'] = answer.get('text','')
                                        row['passed'] = row['expected'] in row['answer']
                                if all(row.get('passed') for row in burst['turns']): break
                                await asyncio.sleep(.25)
                    finally:
                        consumer.cancel()
                        await asyncio.gather(consumer, return_exceptions=True)
                    burst['completedMs'] = round((time.perf_counter()-start)*1000,1)
                    print(json.dumps(burst,ensure_ascii=True),flush=True)
                report['passed'] = True
            traces = SessionJournal(path).entries()
            new_turns = {row['turnId'] for row in report['turns']}
            new_turns.update(row['turnId'] for row in report.get('burst', {}).get('turns', []))
            response_turns = {row['responseId']: (row.get('payload', {}).get('origin') or {}).get('turnId')
                for row in traces if row.get('eventType') == 'response.created' and row.get('responseId')}
            report['textGeneration'] = []
            for row in traces:
                payload = row.get('payload') or {}
                response_id = payload.get('responseId') or row.get('responseId')
                if payload.get('stage') == 'text' and response_turns.get(response_id) in new_turns:
                    report['textGeneration'].append({'turnId': response_turns[response_id],
                        **{key: payload.get(key) for key in ('status', 'firstTokenMs', 'inputMessages',
                            'inputCharacters', 'usage', 'latencyMs')}})
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)[:1000]
    finally:
        profile.disable()
        stream=io.StringIO();pstats.Stats(profile,stream=stream).sort_stats('cumulative').print_stats(35)
        (args.output/'startup-profile.txt').write_text(stream.getvalue(),encoding='utf-8')
        report['hashMismatches'] = [p for p, h in plan['hashes'].items()
            if not (root / p).is_file() or hashlib.sha256((root / p).read_bytes()).hexdigest() != h]
        report['passed'] = report['passed'] and not report['hashMismatches']
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='turns'},ensure_ascii=True),flush=True)
    return report['passed']


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--voice',action='store_true')
    parser.add_argument('--burst',action='store_true')
    parser.add_argument('--receipt-budget-ms',type=float,default=1000)
    parser.add_argument('--first-audio-budget-ms',type=float,default=8000)
    raise SystemExit(0 if asyncio.run(verify(parser.parse_args())) else 1)
