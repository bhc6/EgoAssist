"""Create hashed, video-isolated EgoLife split manifest."""
import argparse, hashlib, json
from pathlib import Path
from egoassist.eval.egolife_experiments import load_egolife_tree, split_by_video

def main():
    p=argparse.ArgumentParser(); p.add_argument('--dataset',type=Path,required=True); p.add_argument('--output',type=Path,required=True); p.add_argument('--seed',type=int,default=0); a=p.parse_args()
    records,samples,meta=load_egolife_tree(a.dataset)
    splits=split_by_video(samples,seed=a.seed)
    def digest(items): return hashlib.sha256('\n'.join(sorted(x.key for x in items)).encode()).hexdigest()
    out={'schema':'egoassist.egolife.split.v2','seed':a.seed,'dataset':meta,'splits':{('reused_validation' if k == 'test' else k):{'count':len(v),'keysSha256':digest(v),'videoIds':sorted({x.video_id for x in v})} for k,v in splits.items() if isinstance(v,list)},'status':'exploratory-video-groups','independentTestQuestions':0,'priorExposure':'existing dataset has already participated in tuning; a new split does not restore independence'}
    a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf8'); print(json.dumps(out,ensure_ascii=False))
if __name__=='__main__': main()
