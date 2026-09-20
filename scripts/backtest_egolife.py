"""Legacy diagnostics only. Use run_controlled_retrieval.py for A-E comparisons."""
import argparse, json
from pathlib import Path
from egoassist.eval.egolife_experiments import TimelineAdapter, VectorAdapter, HybridAdapter, SemanticEmbeddingAdapter, QwenEmbeddingAdapter, aggregate_backtests, evaluate_adapter, load_egolife_tree

def main():
    p=argparse.ArgumentParser(); p.add_argument('--dataset',type=Path,default=Path('data_real/egolife')); p.add_argument('--output',type=Path,required=True); p.add_argument('--seeds',default='0,1,2'); p.add_argument('--max-questions',type=int,default=300); p.add_argument('--enable-local-semantic',action='store_true'); a=p.parse_args(); a.output.mkdir(parents=True,exist_ok=False)
    if a.enable_local_semantic: import os; os.environ['EGOASSIST_ENABLE_LOCAL_SEMANTIC']='1'
    records,samples,manifest=load_egolife_tree(a.dataset,max_questions=a.max_questions); runs=[]
    for seed in [int(x) for x in a.seeds.split(',') if x.strip()]:
        routes=(("timeline", TimelineAdapter(a.output/f'timeline-{seed}.sqlite3')), ("vector", VectorAdapter()), ("hybrid", HybridAdapter(a.output/f'hybrid-{seed}.sqlite3')), ("qwen", QwenEmbeddingAdapter(cache_path=a.output/'qwen-embedding-cache.json')))
        if a.enable_local_semantic: routes += (("semantic", SemanticEmbeddingAdapter()),)
        for label, adapter in routes:
            run=evaluate_adapter(adapter,records,samples,seed=seed); run['seed']=seed; run['route']=label; runs.append(run)
    aggregates={k: aggregate_backtests([r for r in runs if r['route']==k]) for k in ('timeline','vector','hybrid','qwen') if any(r['route']==k for r in runs)}
    if a.enable_local_semantic: aggregates['semantic']=aggregate_backtests([r for r in runs if r['route']=='semantic'])
    validity={'evaluationRole':'legacy-exploratory-diagnostics','independentTest':False,'sharedHardConstraints':False,'claimBoundary':'Vector-only diagnostics are not safe-recall comparisons. Use scripts/run_controlled_retrieval.py for controlled A-E runs.'}
    result={'manifest':manifest,'validity':validity,'runs':runs,'aggregates':aggregates}; (a.output/'backtest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8'); print(json.dumps({'validity':validity,'aggregates':aggregates},ensure_ascii=False))
if __name__=='__main__': main()
