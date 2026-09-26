"""Compare deployed voice paths with identical PCM and real providers.

Measures PCM end to received output, not physical microphone/speaker latency.
Run in an isolated runtime; never changes the running service configuration.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from egoassist.realtime.modular import settings
from run_egoassist import create_app
from real_session_support import journal
from verify_real_skill_lifecycle import connect
from verify_real_voice_stop import decode_fixture, synthesize


async def stream_pcm(ws, pcm, start, row, prefix='input'):
    data = pcm + bytes(32000 * 3)
    lag = 0
    for offset in range(0, len(data), 3200):
        deadline = start + offset / 32000
        await asyncio.sleep(max(0, deadline - time.perf_counter()))
        lag = max(lag, (time.perf_counter() - deadline) * 1000)
        await ws.send_json({'type': 'audio', 'audio': base64.b64encode(data[offset:offset + 3200]).decode()})
    row[prefix + 'MaxSendLagMs'] = round(lag, 1)


async def trial(client, output, pcm, row, expected, stop_pcm=None):
    ws, sender, stop_sender = None, None, None
    sid = (await (await client.post('/api/sessions')).json())['session']['id']
    row.update(sessionId=sid, passed=False, events=[], transcripts=[])
    try:
        connection_started = time.perf_counter()
        ws, _ = await connect(client, sid)
        row['connectionMs'] = round((time.perf_counter() - connection_started) * 1000, 1)
        started = time.perf_counter()
        input_end = started + len(pcm) / 32000
        sender = asyncio.create_task(stream_pcm(ws, pcm, started, row))
        stop_end = None
        async with asyncio.timeout(55):
            while True:
                event = await ws.receive_json()
                kind = event.get('type')
                now = time.perf_counter()
                elapsed = round((now - input_end) * 1000, 1)
                if kind in {'error', 'provider_unavailable'}:
                    raise RuntimeError(str(event.get('message') or event.get('reason')))
                if kind == 'user_transcript':
                    row['transcripts'].append(event.get('text') or event.get('transcript'))
                    row.setdefault('transcriptMs', elapsed)
                if kind in {'audio', 'assistant_transcript'} and event.get('purpose') == 'conversation':
                    response_id = event.get('responseId')
                    row.setdefault('responseId', response_id)
                    if response_id == row['responseId']:
                        if kind == 'audio':
                            row.setdefault('firstAudioMs', elapsed)
                            row['audioPackets'] = row.get('audioPackets', 0) + 1
                            if stop_pcm is not None and stop_sender is None:
                                # Finish the original input stream before starting another one.
                                await sender
                                stop_start = time.perf_counter()
                                stop_end = stop_start + len(stop_pcm) / 32000
                                stop_sender = asyncio.create_task(stream_pcm(ws, stop_pcm, stop_start, row, 'stop'))
                        else:
                            row.setdefault('firstTextMs', elapsed)
                            if event.get('done'):
                                row['answer'] = event.get('text', '')
                if stop_end is not None:
                    if kind == 'playback_stop' and event.get('trigger') == 'semantic-floor-change':
                        row.setdefault('stopToPlaybackStopMs', round((now - stop_end) * 1000, 1))
                    if kind == 'speech_control':
                        row['stopAction'] = event.get('action')
                        row['stopToControlMs'] = round((now - stop_end) * 1000, 1)
                if kind in {'user_transcript', 'speech_floor', 'speech_control', 'playback_stop', 'response_status'}:
                    row['events'].append({**event, 'inputEndMs': elapsed})
                if stop_pcm is None:
                    if (kind == 'response_status' and event.get('responseId') == row.get('responseId')
                            and event.get('status') == 'completed' and row.get('answer')):
                        row['completeMs'] = elapsed
                        await sender
                        row['passed'] = (len(row['transcripts']) == 1 and bool(row.get('audioPackets'))
                                         and any(value in row['answer'] for value in expected))
                        break
                elif row.get('stopAction') and 'stopToPlaybackStopMs' in row:
                    await stop_sender
                    row['passed'] = row['stopAction'] == 'stop-speaking' and len(row['transcripts']) == 2
                    break
    except Exception as exc:
        row['error'] = type(exc).__name__ + ': ' + str(exc)[:600]
    finally:
        for task in (sender, stop_sender):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if ws:
            await ws.close()
        rows = journal(output, sid)
        row['routing'] = [{k: r['decision'].get(k) for k in ('status', 'model', 'latencyMs', 'interaction')}
                          for r in rows if r.get('type') == 'control_decision']
        row['pipeline'] = [r['payload'] for r in rows if r.get('eventType') == 'provider.pipeline.metrics']
        row['responseModels'] = [r.get('payload', {}).get('model') or r.get('model')
                                 for r in rows if r.get('eventType') == 'response.created']
        row['generationToAudioMs'] = [r['latencyMs'] for r in rows
                                      if r.get('eventType') == 'response.first_audio'
                                      and r.get('responseId') == row.get('responseId') and r.get('latencyMs') is not None]
        row['provider'] = [r['payload'] for r in rows if r.get('eventType') == 'provider.session.ready']
        row['passed'] = row['passed'] and bool(row['routing']) and all(r['status'] == 'classified' for r in row['routing'])


async def compare(output, repeats, fixture_dir):
    load_env(str(ROOT / '.env'))
    output.mkdir(parents=True, exist_ok=False)
    previous = os.environ.get('EGOASSIST_REALTIME_BACKEND')
    report = {'realProviders': True, 'hardwareMeasured': False, 'runs': [], 'sources': [],
              'clock': 'PCM end to first received audio; connection excluded; includes endpointing, ASR, JEV and answer',
              'modular': settings(), 'omniModel': 'qwen3.5-omni-flash-realtime',
              'omniSilenceMs': os.getenv('EGOASSIST_SPEECH_END_SILENCE_MS', '1000'),
              'modularSilenceMs': 660, 'passed': False}
    def save():
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    try:
        phrases = ['助手，七加八是多少？请直接回答结果。', '助手，请帮我算一下，',
                   '十九加二十三是多少？请直接回答结果。',
                   '助手，请详细讲解太阳系的八大行星，至少讲二十句话。', '助手，先别说话了。']
        clips = []
        for index, phrase in enumerate(phrases):
            if fixture_dir:
                pcm, metadata = decode_fixture((fixture_dir / f'input-{index}.wav').read_bytes())
            else:
                pcm, metadata = await synthesize(phrase, output, index)
            clips.append(pcm)
            report['sources'].append({'text': phrase, 'kind': 'synthetic-TTS', **metadata})
        cases = [('short', clips[0], ('15', '十五')),
                 ('pause-500ms', clips[1] + bytes(16000) + clips[2], ('42', '四十二'))]
        report['fixtures'] = {name: {'pcmSha256': hashlib.sha256(pcm).hexdigest(), 'durationMs': len(pcm) / 32}
                              for name, pcm, _ in cases}
        save()
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / 'runtime.json'), report['omniModel'], 'Ethan')
        async with TestClient(TestServer(app)) as client:
            assert (await client.post('/api/auth/register', json={'username': 'voice-comparison',
                'password': 'isolated-voice-comparison-123'})).status == 200
            for repeat in range(repeats):
                for name, pcm, expected in cases:
                    for backend in (['omni', 'modular'] if repeat % 2 == 0 else ['modular', 'omni']):
                        os.environ['EGOASSIST_REALTIME_BACKEND'] = backend
                        row = {'backend': backend, 'case': name, 'repeat': repeat + 1}
                        report['runs'].append(row)
                        await trial(client, output, pcm, row, expected)
                        save()
                        print(json.dumps({k: row.get(k) for k in ('backend', 'case', 'repeat', 'passed', 'firstAudioMs', 'transcriptMs', 'answer', 'error')}, ensure_ascii=True), flush=True)
            for backend in ['modular', 'omni']:
                os.environ['EGOASSIST_REALTIME_BACKEND'] = backend
                row = {'backend': backend, 'case': 'voice-stop', 'repeat': 1}
                report['runs'].append(row)
                await trial(client, output, clips[3], row, (), clips[4])
                save()
                print(json.dumps({k: row.get(k) for k in ('backend', 'case', 'passed', 'stopToPlaybackStopMs', 'stopToControlMs', 'error')}), flush=True)
        report['summary'] = {}
        for backend in ['omni', 'modular']:
            trials = [r for r in report['runs'] if r['backend'] == backend and r['case'] != 'voice-stop']
            successful = [r for r in trials if r['passed']]
            summary = {'attempts': len(trials), 'passed': len(successful), 'metricsFromSuccessfulTrialsOnly': True}
            for metric in ['transcriptMs', 'firstAudioMs', 'firstTextMs', 'completeMs']:
                values = [r[metric] for r in successful if metric in r]
                summary[metric] = {'median': round(statistics.median(values), 1), 'min': min(values), 'max': max(values)} if values else None
            report['summary'][backend] = summary
        report['passed'] = all(r['passed'] for r in report['runs'])
    except Exception as exc:
        report['error'] = type(exc).__name__ + ': ' + str(exc)[:600]
    finally:
        if previous is None:
            os.environ.pop('EGOASSIST_REALTIME_BACKEND', None)
        else:
            os.environ['EGOASSIST_REALTIME_BACKEND'] = previous
        save()
    print(json.dumps({'passed': report['passed'], 'summary': report.get('summary'), 'error': report.get('error')}), flush=True)
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--fixture-dir', type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error('--repeats must be between 1 and 10')
    raise SystemExit(0 if asyncio.run(compare(args.output, args.repeats, args.fixture_dir)) else 1)
