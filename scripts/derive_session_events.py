"""Derive events in an existing session journal; keep original sources immutable."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from egoassist._env import load_env
from egoassist.core.journal import SessionJournal
from egoassist.realtime.long_events import LongEvents


async def run(args):
    load_env(str(ROOT/'.env'))
    if not args.journal.is_file():raise ValueError('session journal does not exist')
    args.output.mkdir(parents=True,exist_ok=False)
    journal=SessionJournal(args.journal)
    tracker=LongEvents(journal,os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY'))
    try:
        if not tracker.service.key:raise ValueError('model key is missing')
        await tracker.run(max_batches=20)
        result=tracker.snapshot()
        (args.output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'events':result['totalEvents'],'pendingSources':result['pendingSources'],
                          'jobStatus':(result['job'] or {}).get('status')},ensure_ascii=True))
        return result['pendingSources']==0 and (result['job'] or {}).get('status')!='failed'
    finally:await tracker.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)
