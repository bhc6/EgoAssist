"""Check, launch, or verify the local demonstration in an isolated data root."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist._env import load_env

MEDIA = ROOT / 'data_real/egolife/A1_JAKE_DAY1/DAY1_A1_JAKE_11100000.mp4'


def environment():
    load_env(str(ROOT / '.env'))
    # Explicit demo profile; .env and the regular entrypoint are unchanged.
    return {**os.environ, 'EGOASSIST_REALTIME_BACKEND': 'modular',
            'EGOASSIST_MODULAR_TEXT_MODEL': 'qwen3-max',
            'EGOASSIST_TOOL_CALLS_ENABLED': '1',
            'EGOASSIST_DECISIONS_TIMEOUT': '4.0', 'EGOASSIST_TURN_ROUTER_TIMEOUT': '8.0'}


def preflight(env, *, real=True, media=False):
    checks = []

    def check(name, ok, detail):
        checks.append({'name': name, 'passed': bool(ok), 'detail': detail})

    for name in ('aiohttp', 'dashscope', 'pydantic', 'webrtcvad', 'playwright'):
        check('dependency:'+name, importlib.util.find_spec(name) is not None, 'required Python module')
    edge = shutil.which('msedge') or next((str(p) for p in [
        Path(os.getenv('PROGRAMFILES(X86)', 'C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe',
        Path(os.getenv('PROGRAMFILES', 'C:/Program Files'))/'Microsoft/Edge/Application/msedge.exe',
    ] if p.is_file()), None)
    check('browser:Edge', edge, 'browser acceptance uses installed Edge')
    for name in ('bundle.min.js', 'vad.worklet.bundle.min.js', 'silero_vad_v5.onnx',
                 'ort.wasm.min.js', 'ort-wasm-simd-threaded.wasm', 'ort-wasm-simd-threaded.mjs'):
        p = ROOT/'egoassist/console/vendor/vad'/name
        check('asset:'+name, p.is_file() and p.stat().st_size > 0, 'bundled local voice asset')
    skills = ROOT/'egoassist/skills'
    for name in ('kitchen-coach', 'assembly-coach', 'packing-coach', 'field-notes'):
        folder = skills/name
        try:
            json.loads((folder/'skill.json').read_text(encoding='utf-8'))
            assert (folder/'SKILL.md').is_file()
            for p in (folder/'tasks').glob('*.json'):
                json.loads(p.read_text(encoding='utf-8'))
            check('skill:'+name, True, 'manifest, policy and task JSON readable')
        except (OSError, ValueError, AssertionError):
            check('skill:'+name, False, 'missing or malformed packaged Skill')
    for p in (skills/'assembly-coach/references/assembly-reference.pdf',
              skills/'kitchen-coach/references/kitchen-reference.pdf'):
        check('reference:'+p.name, p.is_file() and p.read_bytes().startswith(b'%PDF'), 'local task reference')
    router = env.get('EGOASSIST_TURN_ROUTER_BACKEND', 'qwen').strip()
    check('router', router in {'qwen', 'openrouter-decisions'}, router)
    if real:
        check('Qwen credential', bool(env.get('QWEN_API_KEY') or env.get('DASHSCOPE_API_KEY')), 'presence only; no network request')
        if router == 'openrouter-decisions':
            check('OpenRouter credential', bool(env.get('OPENROUTER_API_KEY')), 'presence only; no network request')
    if media:
        check('replay video', MEDIA.is_file(), str(MEDIA))
        check('video decoder', importlib.util.find_spec('av') is not None, 'required only for replay acceptance')
    return {'passed': all(c['passed'] for c in checks), 'networkTested': False,
            'profile': {'backend': 'modular', 'textModel': 'qwen3-max', 'tools': True, 'router': router,
                        'decisionTimeoutSeconds': 4, 'routerTimeoutSeconds': 8},
            'checks': checks,
            'optionalConfigured': {
                'temporal': bool(env.get('EGOASSIST_TEMPORAL_TARGET')),
                'helpWebhook': bool(env.get('EGOASSIST_HUMAN_HELP_WEBHOOK')),
                'livekit': all(env.get(k) for k in ('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET'))}}


def verify(output, env, *, real, media):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {'passed': False, 'realProvidersRequested': real, 'hardwareMeasured': False,
              'preflight': preflight(env, real=real, media=media), 'steps': []}

    def save():
        (output/'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    if not report['preflight']['passed']:
        print(json.dumps(report['preflight'], ensure_ascii=False, indent=2))
        return False
    steps = [('browser', 'verify_browser.py', ['--real-turn'] if real else [])]
    if real:
        steps += [
            ('skills', 'verify_real_skill_catalog.py', []),
            ('tasks', 'verify_real_skills.py', []),
            ('memory', 'verify_real_memory.py', []),
            ('diary-browser', 'verify_lanes_diary_browser.py', []),
            ('diary-periods', 'verify_demo_diary.py', []),
            ('source-lifecycle', 'verify_source_lifecycle_real.py', []),
            ('voice', 'verify_real_voice_stop_browser.py', ['--local-vad']),
            ('cross-day', 'verify_cross_day_event.py', []),
            ('cross-day-browser', 'verify_cross_day_event_browser.py', []),
        ]
        if media:
            steps += [('observation', 'verify_open_ambient_real.py', []),
                      ('observation-reminders', 'verify_observation_reminders_real.py', []),
                      ('visual-answer', 'verify_direct_visual_real.py', ['--rounds', '1']),
                      ('daily-dialogue', 'verify_daily_dialogue_browser.py', [])]
    report['excluded'] = [] if media else ['EgoLife replay observation, visual answer and daily-dialogue suite (use --with-media)']
    if not real:
        report['excluded'].append('All cloud model, speech and memory semantics checks (use --real)')
    for name, script, extra in steps:
        print('Checking '+name+'...', flush=True)
        started = time.monotonic()
        row = {'name': name, 'passed': False, 'log': name+'.log'}
        report['steps'].append(row)
        try:
            with (output/row['log']).open('w', encoding='utf-8') as log:
                step_output = output/('cross-day' if name == 'cross-day-browser' else name)
                result = subprocess.run([sys.executable, str(ROOT/'scripts'/script), '--output',
                    str(step_output), *extra], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                    timeout=900, check=False)
            row.update(exitCode=result.returncode, passed=result.returncode == 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            row['error'] = type(exc).__name__
        row['seconds'] = round(time.monotonic()-started, 1)
        save()
        print(name+': '+('PASS' if row['passed'] else 'FAIL (see '+row['log']+')'), flush=True)
    report['passed'] = all(r['passed'] for r in report['steps'])
    save()
    print('Report: '+str(output/'acceptance.json'), flush=True)
    return report['passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check', help='inspect local requirements; never calls cloud models')
    start = commands.add_parser('start', help='start with tools enabled and isolated demo storage')
    start.add_argument('--port', type=int, default=8770)
    start.add_argument('--memory-path', type=Path, default=ROOT/'.egoassist-demo/runtime.json')
    start.add_argument('--no-open', action='store_true')
    acceptance = commands.add_parser('verify', help='serial acceptance; each step preserves its own evidence')
    acceptance.add_argument('--output', type=Path, required=True)
    acceptance.add_argument('--real', action='store_true', help='call real cloud providers (billable)')
    acceptance.add_argument('--with-media', action='store_true', help='include local EgoLife replay; requires --real')
    args = parser.parse_args()
    env = environment()
    if args.command == 'check':
        report = preflight(env)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['passed'] else 1
    if args.command == 'start':
        command = [sys.executable, str(ROOT/'run_egoassist.py'), '--port', str(args.port),
                   '--memory-path', str(args.memory_path.resolve())]
        if not args.no_open:
            command.append('--open')
        print('Demo: modular voice, qwen3-max answers, tools enabled, router budget 8s; data: '+str(args.memory_path.resolve()), flush=True)
        return subprocess.call(command, cwd=ROOT, env=env)
    if args.with_media and not args.real:
        parser.error('--with-media requires --real')
    return 0 if verify(args.output, env, real=args.real, media=args.with_media) else 1


if __name__ == '__main__':
    raise SystemExit(main())
