"""Synthetic speech + authentic saved frames through native Omni microphone input."""
import argparse
import asyncio
import base64
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from verify_real_voice_stop import synthesize


async def verify(source, output, *, pause_ms=0):
    output.mkdir(parents=True, exist_ok=False)
    question = '助手，请简短告诉我，刚才画面里那个人的手有什么变化？'
    if pause_ms:
        first, first_fixture = await synthesize('助手，请简短告诉我，', output, 0)
        last, last_fixture = await synthesize('刚才画面里那个人的手有什么变化？', output, 1)
        pcm = first + bytes(int(pause_ms * 32)) + last
        fixture = {'parts': [first_fixture, last_fixture], 'insertedPauseMs': pause_ms,
                   'durationMs': len(pcm) / 32}
    else:
        pcm, fixture = await synthesize(question, output, 0)
    pcm = bytes(16000) + pcm + bytes(48000)
    rows = [json.loads(s) for s in source.read_text(encoding='utf8').splitlines()]
    observations = [r for r in rows if r['type'] == 'ambient_observation' and
                    '2026-09-18T04:48:10' <= (r.get('evidence') or {}).get('capturedAt', '') <= '2026-09-18T04:48:19']
    assert len(observations) == 2
    report = {'passed': False, 'inputQuestion': question, 'fixture': fixture, 'syntheticSpeech': True,
              'realSavedCameraFrames': True, 'retimedReplay': True, 'physicalHardwareMeasured': False, 'events': []}
    app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / 'runtime.json'), 'qwen3.5-omni-flash-realtime', 'Ethan')
    try:
        async with TestClient(TestServer(app)) as client:
            assert (await client.post('/api/auth/register', json={'username': 'av-microphone', 'password': 'av-microphone-test-123'})).status == 200
            sid = (await (await client.post('/api/sessions')).json())['session']['id'];report['sessionId'] = sid
            async with client.ws_connect('/ws/realtime?session=' + sid) as ws:
                async with asyncio.timeout(60):
                    while (await ws.receive_json()).get('type') != 'ready':pass
                started = time.perf_counter()
                async def send():
                    for offset in range(0, len(pcm), 3200):
                        # Show the prior action at speech start, and the resting
                        # frame one second later, with the same replay clock.
                        if offset in {0, 32000}:
                            row = observations[0 if offset == 0 else 1]
                            ref = row['evidence']['media']
                            data = (source.parent / 'media' / source.stem / ref['assetId']).read_bytes()
                            await ws.send_json({'type': 'video', 'image': base64.b64encode(data).decode(),
                                'capture': {'clockId': 'mic-replay', 'monotonicMs': 10000 + offset / 32,
                                            'originalCapturedAt': row['evidence']['capturedAt']}})
                        chunk = pcm[offset:offset+3200]
                        await ws.send_json({'type': 'audio', 'audio': base64.b64encode(chunk).decode(),
                            'capture': {'clockId': 'mic-replay', 'startMonotonicMs': 10000 + offset / 32,
                                        'endMonotonicMs': 10000 + (offset+len(chunk)) / 32}})
                        await asyncio.sleep(.1)
                sender = asyncio.create_task(send());audio = bytearray()
                try:
                    async with asyncio.timeout(75):
                        while True:
                            event = await ws.receive_json();kind = event.get('type')
                            if kind == 'state':continue
                            event['elapsedMs'] = round((time.perf_counter()-started)*1000, 1)
                            if event.get('audio'):
                                chunk = base64.b64decode(event.pop('audio'));audio.extend(chunk)
                                event['audioBytes'] = len(chunk);report.setdefault('firstAudioFromInputStartMs', round((time.perf_counter()-started)*1000,1))
                            report['events'].append(event)
                            if kind == 'assistant_transcript':
                                report['answer'] = event['text'] if event.get('done') else report.get('answer','') + event.get('text','')
                            assert kind not in {'error', 'provider_unavailable'}, event
                            if kind == 'response_status' and event.get('status') in {'completed','interrupted'}:
                                assert event['status'] == 'completed', event
                                break
                    await sender
                finally:
                    if not sender.done():sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                assert audio and report.get('answer')
                with wave.open(str(output/'answer.wav'),'wb') as wav:
                    wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(audio)
            path = next(output.glob(f'users/*/sessions/{sid}.jsonl'))
            entries = [json.loads(s) for s in path.read_text(encoding='utf8').splitlines()]
            report['visualResults'] = [r for r in entries if r['type']=='scene.question.result']
            report['sceneTurns'] = [r for r in entries if r['type']=='scene.turn']
            report['inputs'] = [r for r in entries if r['type']=='turn_inbox']
            if pause_ms:
                report['pauseCheck'] = {'insertedPauseMs': pause_ms, 'turnCount': len(report['inputs']),
                    'oneCompleteTurn': len(report['inputs']) == 1 and '变化' in report['inputs'][0]['text']}
                assert report['pauseCheck']['oneCompleteTurn'], 'Speech pause split the question'
            def event_at(name):
                return next((datetime.fromisoformat(r['timestamp']) for r in entries if r.get('eventType') == name), None)
            stopped = event_at('input.segment.stopped')
            transcribed = event_at('input.segment.transcribed')
            first_audio = event_at('response.first_audio')
            report['serverTiming'] = {
                'basis': 'server events; speech stopped is provider VAD notification, not physical speech endpoint',
                'vadStopToTranscriptMs': round((transcribed-stopped).total_seconds()*1000, 1) if stopped and transcribed else None,
                'transcriptToFirstAudioMs': round((first_audio-transcribed).total_seconds()*1000, 1) if first_audio and transcribed else None,
                'vadStopToFirstAudioMs': round((first_audio-stopped).total_seconds()*1000, 1) if first_audio and stopped else None,
            }
            assert report['inputs'] and all(r['source']=='input-audio-transcript' for r in report['inputs'])
            assert any(r['status']=='observed' and len(r.get('frames',[]))==2 for r in report['visualResults'])
            report['chainPassed'] = True
            report['behaviorCheck'] = {'expected': 'Earlier hand near face; later hand no longer visible near face.',
                'matched': any(word in report['answer'] for word in ['离开', '放下', '移开', '不再', '垂下', '消失', '不可见', '不在画面', '看不到', '看不见'])}
            assert report['behaviorCheck']['matched'], 'Answer did not describe the observed change; review saved frames and answer'
            report['passed'] = True
    finally:
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'events','sceneTurns','visualResults','inputs'}},ensure_ascii=True))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--journal',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--pause-ms', type=int, default=0)
    args=parser.parse_args();load_env();asyncio.run(verify(args.journal,args.output,pause_ms=args.pause_ms))
