"""Real-model lifecycle test with explicitly synthetic observation fixtures."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from egoassist._env import load_env
from egoassist.core.journal import SessionJournal
from egoassist.realtime.long_events import LongEvents


async def verify(output):
    load_env(str(ROOT/'.env'));output.mkdir(parents=True,exist_ok=False)
    journal=SessionJournal(output/'stages.jsonl',session_id='stage-fixture')
    key=os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY')
    tracker=LongEvents(journal,key)
    start=datetime.now(timezone.utc)-timedelta(hours=3)
    report={'passed':False,'realModel':True,'syntheticObservations':True,'physicalMediaTest':False,'stages':[]}
    def add(minute,observation,statement):
        stamp=(start+timedelta(minutes=minute)).isoformat()
        journal.append('ambient_observation',{'timestamp':stamp,'status':'accepted',
            'fixture':True,'evidence':{'capturedAt':stamp,'visibleEvidence':[observation],'uncertainties':[]}})
        journal.append('message',{'timestamp':stamp,'status':'completed','fixture':True,
            'message':{'role':'user','content':statement}})
    try:
        add(0,'案板上放着番茄和鸡蛋，有人切番茄。','我现在开始做今天的番茄炒蛋晚饭。')
        await tracker.run();first=tracker.snapshot();assert first['events'];eid=first['events'][0]['eventId']
        report['stages'].append(first)
        add(5,'锅放在未点火的灶台上，切好的番茄仍在案板上。','这顿饭先暂停，我要接电话，还没有炒。')
        await tracker.run();paused=tracker.snapshot();report['stages'].append(paused)
        assert any(e['eventId']==eid and e['status']=='paused' for e in paused['events'])
        # Restart the actual component between stages.
        await tracker.close();tracker=LongEvents(SessionJournal(journal.path),key)
        add(12,'有人用锅铲翻炒锅里的番茄和鸡蛋。','电话打完了，我继续做刚才那顿饭，现在开始炒。')
        await tracker.run();resumed=tracker.snapshot();report['stages'].append(resumed)
        assert any(e['eventId']==eid and e['status']=='ongoing' for e in resumed['events'])
        add(18,'盛好的番茄炒蛋放在餐桌上，灶台未见火焰。','这顿番茄炒蛋已经做完并端上桌了。')
        await tracker.run();done=tracker.snapshot();report['stages'].append(done)
        event=next(e for e in done['events'] if e['eventId']==eid)
        assert event['status']=='completed' and len(event['phases'])>=4
        assert event['coverage']['continuousDurationSeconds'] is None
        assert event['coverage']['gapCount']>0
        add(100,'有人再次在案板上切番茄。','现在给家人另做一份番茄炒蛋，这是新的一次。')
        await tracker.run();later=tracker.snapshot();report['stages'].append(later)
        assert len(later['events'])>=2 and next(e for e in later['events'] if e['eventId']==eid)['status']=='completed'
        assert later['pendingSources']==0
        report['providerRequestIds']=[r['providerRequestId'] for r in journal.entries(types={'long_event.response'})]
        assert len(report['providerRequestIds'])==5 and all(report['providerRequestIds'])
        report['passed']=True
    except Exception as exc:
        report['error']=type(exc).__name__+': '+str(exc)
    finally:
        await tracker.close()
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='stages'},ensure_ascii=True))
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();raise SystemExit(0 if asyncio.run(verify(args.output)) else 1)
