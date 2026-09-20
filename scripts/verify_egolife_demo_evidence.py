"""Cross-check each spoken turn against streamed frames and original response audio."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    args=parser.parse_args();folder=args.source.resolve()
    report=json.loads((folder/'report.json').read_text(encoding='utf-8'))
    case=report['cases'][0]
    journal=json.loads((folder/'journals/egolife.json').read_text(encoding='utf-8'))
    feed=[json.loads(l) for l in (folder/'frame-feed.jsonl').read_text(encoding='utf-8').splitlines()]
    by_hash={r['sha256']:r for r in feed}
    events=[json.loads(l) for l in (folder/'browser-events.jsonl').read_text(encoding='utf-8').splitlines()]
    rows=[]
    for step in case['steps']:
        tid=step['turnId']
        scene=next(r for r in reversed(journal) if r.get('type')=='scene.turn' and r.get('turnId')==tid)
        ref=scene.get('frame') or {}
        assert ref.get('sha256') in by_hash, f'No streamed frame matched turn {tid}'
        assert scene.get('audio'), f'No captured audio evidence for {tid}'
        assert step['turn']['source']=='input-audio-transcript'
        assert step['turn']['delivery']['status']=='generated'
        assert step['audio'] and step['responseIds']
        answers=[e for e in events if e.get('type')=='assistant_transcript' and e.get('done')
                 and e.get('origin',{}).get('turnId')==tid]
        assert answers, f'No original spoken transcript for {tid}'
        assert any(e.get('text')==step['answer'] for e in answers), f'Written/spoken mismatch {tid}'
        for name in step['audio']:
            assert (folder/name).stat().st_size>1000
        rows.append({'turnId':tid,'recognizedText':step['turn']['text'],'answer':step['answer'],
                     'responseIds':step['responseIds'],'frameSha256':ref['sha256'],
                     'sourceSeconds':by_hash[ref['sha256']]['sourceSeconds'],
                     'synchronizedFrameCount':len(scene.get('synchronizedFrames',[])),
                     'audioInputSha256':scene['audio']['sha256'],
                     'outputAudio':[{'file':name,'sha256':hashlib.sha256((folder/name).read_bytes()).hexdigest()} for name in step['audio']]})
    result={'passed':True,'turnCount':len(rows),'framesSent':len(feed),
            'distinctFrames':len(by_hash),'checks':rows,
            'scope':'Transport and evidence correspondence; not a guarantee of visual semantic correctness.'}
    (folder/'multimodal-evidence-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'turns':len(rows),'frames':len(feed),'distinct':len(by_hash)}),flush=True)


if __name__=='__main__':
    main()
