"""Replay real observations through production historical vision and answer paths.

No QA gold or target fields enter model context. This is a diagnostic replay,
not a new browser recording or a full benchmark. Preserve unsuccessful results.
"""
import argparse
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import av
from egoassist._env import load_env
from egoassist.core.journal import SessionJournal
from egoassist.realtime.scene import SharedScene
from egoassist.product.answer_jobs import AnswerJobs
from egoassist.eval.choice import explicit_choice
from run_egolife_benchmark_demo import OFFICIAL, public, prompt


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


async def _run(args, resources):
    load_env(str(ROOT/'.env'));key=os.getenv('QWEN_API_KEY') or os.getenv('DASHSCOPE_API_KEY')
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    source=ROOT/'outputs/egolife-benchmark-long-20260916-r1'
    old=json.loads((source/'report.json').read_text('utf-8'))
    questions={r['ID']:public(r) for r in json.loads(OFFICIAL.read_text('utf-8')) if r['ID'] in ['81','102']}
    report={'protocol':'actual production component replay; old input vs historical reinspection; gold scoring only',
            'questions':questions,'results':[],'rawVideoSource':str(args.extra_video) if args.extra_video else None,
            'restoredMedia': {'path': str(args.restore_media.resolve()),
                              'sha256': hashlib.sha256(args.restore_media.read_bytes()).hexdigest()}
                             if args.restore_media else None}
    save(out/'report.json',report)
    for qid in args.ids.split(','):
        case=next(c for c in old['cases'] if c['question']['ID']==qid)
        sid=case['sessionId'];original=next((ROOT/'.egoassist-egolife-demo/users').glob(f'*/sessions/{sid}.jsonl'))
        journal=SessionJournal(out/(qid+'.jsonl'),session_id=qid)
        scene=SharedScene(journal,key)
        resources.append(scene)
        observations=[r for r in json.loads((source/'journals'/f'q{qid}.json').read_text('utf-8')) if r.get('type')=='ambient_observation' and r.get('status')=='accepted']
        base=datetime.now(timezone.utc)-timedelta(minutes=12)
        start=datetime.fromisoformat(observations[0]['evidence']['capturedAt'])
        for r in observations:
            e=dict(r['evidence']);ref=e.get('media')
            if not ref:continue
            data=(original.parent/'media'/sid/ref['assetId']).read_bytes()
            assert hashlib.sha256(data).hexdigest()==ref['sha256']
            if args.restore_media:
                feed=next(f for f in case['frameFeed'] if f['sha256']==ref['sha256'])
                with av.open(str(args.restore_media)) as container:
                    container.seek(int(feed['sourceSeconds']*1e6))
                    f=next(f for f in container.decode(video=0) if f.time>=feed['sourceSeconds'])
                    import io
                    picture=f.to_image();picture.thumbnail((1024,1024));b=io.BytesIO();picture.save(b,format='JPEG',quality=90)
                    data=b.getvalue()
                ref={**ref,**scene._save(data,'.jpg','image/jpeg')};e['media']=ref
            else:
                scene._save(data,'.jpg','image/jpeg')
            e['capturedAt']=(base+(datetime.fromisoformat(e['capturedAt'])-start)).isoformat()
            row=journal.append('ambient_observation',{'status':'accepted','evidence':e,'originalSourceId':r['id']})
            scene.ingest_observation(row)
        if args.extra_video and qid=='102':
            # Cover the entire additional 30-second source, not the gold frame.
            with av.open(str(args.extra_video)) as container:
                points=[];next_time=0
                for f in container.decode(video=0):
                    if f.time is not None and f.time>=next_time:
                        import io
                        picture=f.to_image();picture.thumbnail((768,768));b=io.BytesIO();picture.save(b,format='JPEG',quality=85)
                        points.append((f.time,b.getvalue()));next_time+=3
            for i,(seconds,data) in enumerate(points):
                ref={**scene._save(data,'.jpg','image/jpeg'),'frameId':f'extra-{i}',
                     'receivedAt':datetime.now(timezone.utc).isoformat()}
                scene.vision_progress[ref['frameId']]={'status':'queued'}
                observation=await scene._describe(ref)
                journal.append('replay.video.frame',{'frame':ref,'sourceFile':str(args.extra_video),'sourceSeconds':seconds,
                                                     'sourceSha256':hashlib.sha256(args.extra_video.read_bytes()).hexdigest()})
                if observation['status']!='observed':raise RuntimeError(observation)
                row=journal.append('ambient_observation',{'status':'accepted','evidence':{
                    'capturedAt':ref['receivedAt'],'media':ref,'visibleEvidence':[observation['description']],
                    'uncertainties':['Supplemental raw-video model observation; not human annotation']}})
                scene.ingest_observation(row)
        text=prompt(questions[qid]);scene.capture('question-'+qid,explicit=True)
        context=await scene.context('question-'+qid,describe_frame=True,question=questions[qid]['question_chinese'],allow_history=True)
        if context['historicalVisual']['status']!='observed':raise RuntimeError(context['historicalVisual'])
        jobs=AnswerJobs(key)
        resources.append(jobs)
        result=await jobs._generate(journal,{'turnId':'question-'+qid,'question':text,
            'instructions':'Use supplied visual evidence, including historicalVisual, to answer this question. Historical evidence is not current state. Do not infer identities from options.',
            'context':{'sharedScene':context}})
        prediction={'qid':qid,'answer':result['text'],'providerRequestId':result['providerRequestId'],
                    'historicalEvidence':context['historicalVisual'],'model':result['model']}
        save(out/f'prediction-{qid}.json',prediction)
        official=next(r for r in json.loads(OFFICIAL.read_text('utf-8')) if r['ID']==qid)
        choice=explicit_choice(result['text'])
        report['results'].append({**prediction,'choice':choice,'gold':official['answer'],'correct':choice==official['answer'],
                                  'predictionSha256':hashlib.sha256((out/f'prediction-{qid}.json').read_bytes()).hexdigest()})
        save(out/'report.json',report)
        print(json.dumps({'qid':qid,'answer':result['text'],'choice':choice,'correct':choice==official['answer']},ensure_ascii=True),flush=True)
        await scene.close();await jobs.close()


async def run(args):
    resources = []
    try:
        await _run(args, resources)
    except Exception as exc:
        if args.output.is_dir() and resources:
            save(args.output / 'failure.json', {'type': type(exc).__name__, 'error': str(exc)[:300]})
        raise
    finally:
        await asyncio.gather(*(resource.close() for resource in resources), return_exceptions=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--ids',default='81,102')
    p.add_argument('--extra-video',type=Path)
    p.add_argument('--restore-media',type=Path)
    asyncio.run(run(p.parse_args()))
