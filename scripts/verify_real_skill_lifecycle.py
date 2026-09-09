"""Real Qwen lifecycle acceptance across websocket and application restarts.

Uses text controls and provider audio, not physical glasses or camera completion.
Every attempt writes its own report, including failures; no retries hide errors.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp.test_utils import TestClient, TestServer
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist._env import load_env
from run_egoassist import create_app
from real_session_support import journal, turn


async def connect(client, sid):
    ws = await client.ws_connect('/ws/realtime?session=' + sid)
    try:
        async with asyncio.timeout(30):
            ready = False
            while True:
                event = await ws.receive_json()
                assert event.get('type') not in {'error', 'provider_unavailable'}, event
                if event.get('type') == 'ready':
                    assert event.get('lanes', {}).get('dialogue', {}).get('connected'), event
                    ready = True
                if ready and event.get('type') == 'state':
                    return ws, event['state']
    except BaseException:
        await ws.close()
        raise


def check_state(state, skill=None, task=None, checkpoint=None, paused=False):
    actual = state.get('task') or {}
    assert (state.get('activeSkill') or {}).get('name') == skill
    assert (actual.get('task') or {}).get('id') == task
    assert actual.get('currentCheckpointId') == checkpoint
    assert actual.get('paused', False) == paused
    context = state['promptContext']['text']
    if skill is None:
        assert state['task'] is None
        assert state['artifacts'] == []
        assert '<skill name=' not in context
        assert '<runtime_state>' not in context
    return {
        'activeSkill': skill, 'task': task, 'checkpoint': checkpoint, 'paused': paused,
        'promptSkill': state['promptContext']['skill'],
        'artifacts': [item['artifact_id'] for item in state['artifacts']],
        'routing': state['intentRouting'],
    }


async def verify(output, model):
    load_env(str(ROOT / '.env'))
    output.mkdir(parents=True, exist_ok=False)
    report = {'mock': False, 'model': model, 'input': 'text-control',
              'physicalCompletionMeasured': False, 'passed': False, 'turns': [], 'restores': []}
    credentials = {'username': 'lifecycle-acceptance', 'password': 'isolated-lifecycle-test-123'}
    sid = None

    def app():
        return create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / 'runtime.json'), model, 'Ethan')

    async def exercise(client, ws, text, *expected):
        result = {'input': text, 'passed': False}
        report['turns'].append(result)
        result.update(await turn(ws, output, sid, text))
        state = await (await client.get('/api/state?session=' + sid)).json()
        result['observedState'] = {
            'activeSkill': state.get('activeSkill'), 'task': state.get('task'),
            'routing': state.get('intentRouting'),
        }
        result['state'] = check_state(state, *expected)
        result['passed'] = True
        print(json.dumps({key: result[key] for key in ('input', 'passed', 'firstAudioMs', 'transcript')},
                         ensure_ascii=True), flush=True)

    try:
        async with TestClient(TestServer(app())) as client:
            assert (await client.post('/api/auth/register', json=credentials)).status == 200
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            report['sessionId'] = sid
            ws, state = await connect(client, sid)
            check_state(state)
            async with ws:
                await exercise(client, ws, '我要做番茄炒蛋，请告诉我第一步。',
                               'kitchen-coach', 'tomato-eggs', 'prepare')
                await exercise(client, ws, '我确认已经准备好鸡蛋、番茄、油和盐，这一步完成了。',
                               'kitchen-coach', 'tomato-eggs', 'wash')
                await exercise(client, ws, '暂停当前任务。',
                               'kitchen-coach', 'tomato-eggs', 'wash', True)

        async with TestClient(TestServer(app())) as client:
            assert (await client.post('/api/auth/login', json=credentials)).status == 200
            ws, state = await connect(client, sid)
            report['restores'].append(check_state(state, 'kitchen-coach', 'tomato-eggs', 'wash', True))
            async with ws:
                await exercise(client, ws, '继续当前任务。', 'kitchen-coach', 'tomato-eggs', 'wash')
                await exercise(client, ws, '退出做菜模式。')
                await exercise(client, ws, '番茄炒蛋这四个字一共有几个汉字？我只是问字数，不是要做菜。')
                await exercise(client, ws, '帮我写一句给前台的取物说明。')
                await exercise(client, ws, '我要做番茄炒蛋，请从头开始。',
                               'kitchen-coach', 'tomato-eggs', 'prepare')
                await exercise(client, ws, '我要组装凳子，请告诉我第一步。',
                               'assembly-coach', 'flat-pack-stool', 'inventory')
                await exercise(client, ws, '退出当前 Skill。')

        async with TestClient(TestServer(app())) as client:
            assert (await client.post('/api/auth/login', json=credentials)).status == 200
            ws, state = await connect(client, sid)
            report['restores'].append(check_state(state))
            async with ws:
                await exercise(client, ws, '你好，请用一句话问候我。')
            response = await client.post('/api/skills/activate', json={'sessionId': sid, 'skill': 'assembly-coach'})
            check_state(await response.json(), 'assembly-coach')
            response = await client.post('/api/skills/activate', json={'sessionId': sid, 'skill': ''})
            report['apiExit'] = check_state(await response.json())
            ws, state = await connect(client, sid)
            report['restores'].append(check_state(state))
            await ws.close()
        report['passed'] = True
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if sid:
            rows = journal(output, sid)
            report['routing'] = [r['decision'] for r in rows if r.get('type') == 'control_decision']
        (output / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'passed': report['passed'], 'error': report.get('error'), 'output': str(output)}), flush=True)
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='qwen3.5-omni-flash-realtime')
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(verify(args.output, args.model)) else 1)
