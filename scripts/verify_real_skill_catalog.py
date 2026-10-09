"""Exercise original everyday Skills through production HTTP/WS and real Qwen.

Dictated scenarios are test-user statements, not a recorded dataset or camera truth.
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
from verify_real_skill_lifecycle import check_state, connect


async def verify(output, model):
    load_env(str(ROOT / '.env'))
    output.mkdir(parents=True, exist_ok=False)
    report = {'mock': False, 'model': model, 'input': 'text-control', 'passed': False,
              'physicalCompletionMeasured': False, 'turns': []}
    sid = None
    scenarios = [
        ('请用一句话解释浏览器 cookie 是什么。', None, None, None, False),
        ('帮我做通勤出门核对，今天只带手机、钥匙和门禁卡，请告诉我第一步。',
         'packing-coach', 'commute-check', 'list', False),
        ('我确认清单就是手机、钥匙和门禁卡，清单确认这一步完成了。',
         'packing-coach', 'commute-check', 'gather', False),
        ('我确认手机、钥匙和门禁卡都已经找到，找齐物品这一步完成了。',
         'packing-coach', 'commute-check', 'carry', False),
        ('我确认手机、钥匙和门禁卡都已放进包里，携带核对这一步完成了。',
         'packing-coach', 'commute-check', 'carry', True),
        ('退出当前 Skill。', None, None, None, False),
        ('请整理现场记录：仓库收到六箱配件，其中一箱外包装潮湿，里面有没有损坏还没有检查。',
         'field-notes', None, None, False),
        ('更正刚才的现场记录：总数是七箱，不是六箱；其他情况不变。请重新给出记录草稿。',
         'field-notes', None, None, False),
        ('退出当前 Skill。', None, None, None, False),
        ('帮我写一句给前台的取物说明。', None, None, None, False),
    ]
    try:
        app = create_app(str(BUILTIN_SKILLS_ROOT), None, str(output / 'runtime.json'), model, 'Ethan')
        async with TestClient(TestServer(app)) as client:
            assert (await client.post('/api/auth/register', json={
                'username': 'catalog-acceptance', 'password': 'isolated-catalog-test-123',
            })).status == 200
            sid = (await (await client.post('/api/sessions')).json())['session']['id']
            report['sessionId'] = sid
            ws, initial = await connect(client, sid)
            report['catalog'] = initial['skills']
            async with ws:
                for text, skill, task, checkpoint, completed in scenarios:
                    result = {'input': text, 'passed': False}
                    report['turns'].append(result)
                    result.update(await turn(ws, output, sid, text))
                    answer = result['transcript'].strip()
                    assert answer and answer != text.strip(), 'Assistant echoed the current user statement'
                    if text == '退出当前 Skill。':
                        assert not any(value in answer for value in ('可以出门', '携带核对', '当前任务已完成')), answer
                        if len(report['turns']) > 1 and '现场记录' in report['turns'][-2]['input']:
                            assert '通勤' not in answer, answer
                    if text == '帮我写一句给前台的取物说明。':
                        assert any(value in answer for value in ('前台', '领取', '取物', '取件', '拿取')), answer
                        assert not any(value in answer.lower() for value in ('cookie', '浏览器')), answer
                        assert not any(value in answer for value in ('手机', '钥匙', '门禁卡', '纸箱', '配件', '七箱', '棕色')), answer
                        assert not any(value in answer for value in ('里面有', '贴有', '会议资料', '放在桌上')), answer
                    # Assert the actual text-provider request, not only routing state.
                    requests = [r['payload'] for r in journal(output, sid)
                                if r.get('eventType') == 'provider.model.request'
                                and r.get('payload', {}).get('stage') == 'text']
                    assert requests, 'No audited text-provider request'
                    messages = requests[-1]['request']['input']['messages']
                    users = [m for m in messages if m.get('role') == 'user']
                    assert users and text in str(users[-1]['content']), 'Current user turn missing from provider context'
                    result['providerUserTurns'] = len(users)
                    state = await (await client.get('/api/state?session=' + sid)).json()
                    result['observed'] = {'skill': state['activeSkill'], 'task': state['task']}
                    result['state'] = check_state(state, skill, task, checkpoint)
                    assert (state['task'] or {}).get('completed', False) == completed
                    manifest = state['promptContext']['harnesses'][0]['manifest']
                    layers = {item['name']: item for item in manifest['layers']}
                    result['contextLayers'] = [{key: item[key] for key in ('name', 'source', 'tokens', 'required')}
                                               for item in layers.values()]
                    assert '<skill name=' not in layers['core-policy']['content']
                    assert ('active-skill-policy' in layers) == bool(skill)
                    if skill:
                        assert state['activeSkill']['contentHash'] in layers['active-skill-policy']['source']
                    if task:
                        assert layers['skill-runtime-state']['required']
                        assert not any(value in result['transcript'] for value in ('我看到', '我看见', '画面中')), result['transcript']
                        assert not any(value in result['transcript'] for value in ('钱包', '耳机', '雨伞')), result['transcript']
                    if skill == 'field-notes':
                        answer = result['transcript']
                        expected_number = ('七', '7') if '更正' in text else ('六', '6')
                        assert any(value in answer for value in expected_number), answer
                        assert '潮湿' in answer or '湿' in answer, answer
                        assert any(value in answer for value in ('未', '不确定', '未知', '待', '没')), answer
                        assert not any(value in answer for value in ('已提交', '已发送', '已保存到长期', '请选择任务')), answer
                    result['passed'] = True
                    print(json.dumps({key: result[key] for key in ('input', 'passed', 'firstAudioMs', 'transcript')},
                                     ensure_ascii=True), flush=True)
            report['passed'] = True
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if sid:
            report['routing'] = [row['decision'] for row in journal(output, sid)
                                 if row.get('type') == 'control_decision']
        (output / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'passed': report['passed'], 'error': report.get('error'), 'output': str(output)}), flush=True)
    return report['passed']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='qwen3.5-omni-flash-realtime')
    args = parser.parse_args()
    sys.exit(0 if asyncio.run(verify(args.output, args.model)) else 1)
