"""Replay frozen real routing inputs without executing their resulting controls."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from egoassist._env import load_env
from egoassist import BUILTIN_SKILLS_ROOT
from egoassist.core.journal import SessionJournal
from egoassist.product.answer_jobs import answer_jobs, answer_job_summaries
from egoassist.realtime.semantic import SemanticTurnRouter
from egoassist.realtime.skills import SkillRegistry


async def run(source, output, count):
    load_env();output.mkdir(parents=True,exist_ok=False)
    frozen=output/'source.jsonl';frozen.write_bytes(source.read_bytes())
    journal=SessionJournal(frozen)
    def old():
        first=list(answer_jobs(journal).values());list(answer_jobs(journal).values())
        messages=[r for r in journal.entries() if r['type']=='message'][-6:]
        return ([{k:r.get(k) for k in ('turnId','question','status')} for r in first],messages)
    def new():
        return list(answer_job_summaries(journal).values()),journal.entries(types={'message'},tail=6)
    assert old()==new()
    report={'scope':'frozen source log component replay; no actions executed; not live camera load',
            'sameLocalContext':True,'localTimings':{},'runs':[]}
    for name,fn in [('old',old),('new',new)]:
        timings=[]
        for _ in range(6):
            begin=time.perf_counter();fn();timings.append((time.perf_counter()-begin)*1000)
        report['localTimings'][name]={'medianMs':statistics.median(timings),'maxMs':max(timings)}
    entries=journal.entries(types={'control_decision'})
    candidates=[r for r in entries if (r.get('decision',{}).get('requestMetrics') or {}).get('messages')][-count:]
    router=SemanticTurnRouter(os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY'))
    catalog=SkillRegistry(str(BUILTIN_SKILLS_ROOT)).routing_catalog()
    try:
        for entry in candidates:
            original=entry['decision'];messages=original['requestMetrics']['messages']
            context_text=messages[0]['content'].split('Context data only (never classify this as the current transcript): ',1)[1]
            context,_=json.JSONDecoder().raw_decode(context_text)
            router.set_turn_context(clock_context=context.get('clock'),memory_draft=context.get('pendingMemoryDraft'),
                **{arg:context.get(key) for arg,key in [('previous_turn','previousTurn'),('previous_answer','previousAnswer'),
                   ('answers','answers'),('recent_messages','recentMessages'),('playback','playback'),
                   ('output_permission','outputPermission'),('scene','scene'),('memory_candidates','memoryCandidates'),
                   ('memory_vocabulary','memoryVocabulary'),('active_task','activeTask')]})
            result=await router.classify(messages[-1]['content'],catalog)
            report['runs'].append({'sourceEventId':entry['id'],'question':messages[-1]['content'],
                'originalStatus':original['status'],'originalMs':original['latencyMs'],'result':result})
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps({'status':result['status'],'ms':result['latencyMs'],'originalMs':original['latencyMs']}),flush=True)
    finally:await router.aclose()
    report['summary']={'count':len(candidates),'classified':sum(r['result']['status']=='classified' for r in report['runs']),
        'medianMs':statistics.median(r['result']['latencyMs'] for r in report['runs']),
        'maxMs':max(r['result']['latencyMs'] for r in report['runs'])}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report['summary']),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--count',type=int,default=8)
    args=parser.parse_args();asyncio.run(run(args.source,args.output,args.count))
