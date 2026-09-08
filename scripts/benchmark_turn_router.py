"""Real routing latency and behavior checks; no model mocks or answer injection."""
import argparse
import asyncio
import json
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist._env import load_env
from egoassist.realtime.semantic import SemanticTurnRouter

CATALOG = [{'name': 'kitchen-coach', 'description': 'Cooking guidance and kitchen tasks'},
           {'name': 'assembly-coach', 'description': 'Furniture assembly guidance'}]
CASES = [
    ('greeting', '你好，请用一句话介绍你自己。', {'selectedSkill': None, 'memoryKind': None}),
    ('save', '记住：我的储物柜编号是7429。', {'memoryKind': 'fact', 'memoryConsent': 'explicit'}),
    ('query', '我的储物柜编号是什么？', {'memoryQuery': 'referential', 'memoryKind': None}),
    ('stop', '请停止说话。', {'speechControl': 'stop-speaking'}),
    ('negated-stop', '不要停止说话，请继续介绍。', {'speechControl': None}),
    ('skill', '我要开始做番茄炒蛋，请指导我。', {'selectedSkill': 'kitchen-coach'}),
    ('compound', '五分钟后提醒我关烤箱，现在告诉我烤土豆该用多少度。', {'intentKind': 'background-reminder'}),
    ('draft', '帮我记住小李的电话号码。', {'memoryKind': 'capture'}),
]


async def run(output, repeats):
    load_env(str(ROOT / '.env'))
    output.mkdir(parents=True, exist_ok=False)
    router = SemanticTurnRouter(os.environ['QWEN_API_KEY'])
    rows = []
    try:
        for repeat in range(repeats):
            for name, text, expected in CASES:
                router.set_turn_context(clock_context=None, memory_draft=None,
                                        previous_turn={'turnId': 'prior', 'text': '记住：我的储物柜编号是7429。'})
                result = await router.classify(text, CATALOG)
                checks = {key: result.get(key) == value for key, value in expected.items()}
                if name == 'compound':
                    kinds = {item.get('kind') for item in result.get('intents', [])}
                    checks = {'decomposed': {'background-reminder', 'foreground-dialogue'} <= kinds,
                              'delay': any(item.get('parameters', {}).get('delaySeconds') == 300
                                           for item in result.get('intents', []))}
                if name == 'save':
                    checks['value'] = any(atom.get('value') == '7429' for atom in result.get('memoryAtoms', []))
                passed = result['status'] == 'classified' and all(checks.values())
                row = {'repeat': repeat, 'case': name, 'passed': passed, 'checks': checks, 'result': result}
                rows.append(row)
                with (output / 'results.jsonl').open('a', encoding='utf-8') as f:
                    f.write(json.dumps(row, ensure_ascii=False) + '\n')
                print(json.dumps({'case': name, 'passed': passed, 'ms': result['latencyMs'],
                                  'status': result['status'], 'checks': checks}), flush=True)
    finally:
        if hasattr(router, 'aclose'):
            await router.aclose()
    timings = sorted(row['result']['latencyMs'] for row in rows)
    summary = {'mock': False, 'model': router.model, 'n': len(rows),
               'passed': sum(row['passed'] for row in rows),
               'unavailable': sum(row['result']['status'] == 'unavailable' for row in rows),
               'medianMs': statistics.median(timings), 'maxMs': max(timings)}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)
    return summary['passed'] == summary['n']


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--repeats', type=int, default=2)
    args = p.parse_args()
    if args.repeats < 1:
        p.error('--repeats must be positive')
    sys.exit(0 if asyncio.run(run(args.output, args.repeats)) else 1)
