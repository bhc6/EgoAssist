"""Real provider A/B of routing controls; no actions or memory writes executed."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from egoassist._env import load_env
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.realtime.skills import SkillRegistry
from egoassist import BUILTIN_SKILLS_ROOT


CASES = [
    ('greeting', '助手，你好。', 'reply'),
    ('stop', '助手，请停止说话。', 'stop'),
    ('negated-stop', '助手，不要停止说话，请继续介绍。', 'reply'),
    ('quoted-stop', '助手，他刚才说“别说了”，这句话是什么意思？', 'reply'),
    ('save', '助手，帮我记住，我的储物柜编号是7429。', 'save'),
    ('recall', '助手，我的储物柜编号是什么？', 'recall'),
    ('other-person', '小李，你把桌上的杯子递给我。', 'listen'),
    ('compound', '助手，五分钟后提醒我关烤箱，现在告诉我烤土豆该用多少度。', 'compound'),
    ('task-completed', '助手，我确认已经准备好鸡蛋、番茄、油和盐，这一步完成了。', 'confirm'),
    ('task-question', '助手，这一步完成了吗？', 'no-confirm'),
    ('task-incomplete', '助手，我还没有完成当前步骤。', 'no-confirm'),
    ('task-pause', '助手，把当前做菜任务暂停，但你可以继续回答问题。', 'pause-task'),
]


def assess(result, expected):
    interaction = result.get('interaction') or {}
    action = interaction.get('action')
    stop = result.get('speechControl') == 'stop-speaking' or action in {'listen', 'pause', 'cancel'}
    checks = {'classified': result.get('status') == 'classified'}
    if expected == 'reply':
        checks['reply'] = interaction.get('audience') == 'assistant' and action in {'reply', 'resume'}
        checks['no-stop'] = not stop
    elif expected == 'stop': checks['stop'] = stop and interaction.get('output') == 'silent'
    elif expected == 'listen': checks['not-addressed'] = interaction.get('audience') == 'other' and interaction.get('output') == 'silent'
    elif expected == 'save':
        checks['explicit-save'] = any(e.get('consent') == 'explicit' and e.get('scope') == 'participant'
            for e in result.get('memoryAtoms', []) + result.get('memoryEvents', []))
    elif expected == 'recall':
        checks['retrieval'] = bool(result.get('memoryQuerySpec') or result.get('memoryQuery') or
            any(i.get('kind') == 'foreground-dialogue' and i.get('text') == '我的储物柜编号是什么？' for i in result.get('intents', [])))
    elif expected == 'compound':
        intents = result.get('intents') or []
        checks['decomposition'] = {'background-reminder','foreground-dialogue'} <= {i['kind'] for i in intents}
        checks['delay'] = any(i.get('parameters',{}).get('delaySeconds') == 300 for i in intents)
    elif expected == 'confirm': checks['confirm'] = result.get('taskControl') == 'confirm-step'
    elif expected == 'no-confirm': checks['no-confirm'] = result.get('taskControl') != 'confirm-step'
    elif expected == 'pause-task': checks['pause-task'] = result.get('taskControl') == 'pause-task' and not stop
    return checks


async def run(output, models, repeats):
    load_env(); output.mkdir(parents=True, exist_ok=False)
    routers = {model: SemanticTurnRouter(os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY'), model=model) for model in models}
    catalog = SkillRegistry(str(BUILTIN_SKILLS_ROOT)).routing_catalog()
    report = {'scope':'real text router with microphone context; no executed actions, not hardware latency', 'runs':[]}
    try:
        for repeat in range(repeats):
            for index, (name, text, expected) in enumerate(CASES):
                for model in (models if (index+repeat)%2 == 0 else models[::-1]):
                    router = routers[model]
                    task = {'currentCheckpointId':'ingredients', 'currentStep':{'id':'ingredients','title':'准备食材',
                        'instruction':'准备鸡蛋、番茄、油和盐', 'completion':'用户确认食材准备齐全', 'status':'active'},
                        'paused':False, 'completed':False} if name.startswith('task-') else None
                    router.set_turn_context(clock_context=None, memory_draft=None,
                        scene={'speaker':{'status':'unknown','wearer':False}}, active_task=task)
                    result = await router.classify(text, catalog)
                    checks = assess(result, expected)
                    row = {'repeat':repeat, 'case':name, 'text':text, 'model':model, 'checks':checks,
                           'passed':all(checks.values()), 'result':result}
                    report['runs'].append(row)
                    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
                    print(json.dumps({'case':name,'model':model,'passed':row['passed'],'ms':result['latencyMs'],'checks':checks}),flush=True)
    finally:
        for router in routers.values(): await router.aclose()
    report['summary'] = {model:{'count':len(rs := [r for r in report['runs'] if r['model']==model]),
        'passed':sum(r['passed'] for r in rs),'medianMs':statistics.median(r['result']['latencyMs'] for r in rs),
        'maxMs':max(r['result']['latencyMs'] for r in rs)} for model in models}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report['summary']),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--models',nargs='+',default=['qwen3-max','qwen-flash']);parser.add_argument('--repeats',type=int,default=1)
    args=parser.parse_args();asyncio.run(run(args.output,args.models,args.repeats))
