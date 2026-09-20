import json
from egoassist.eval.egolife_experiments import load_egolife, TimelineAdapter, evaluate_adapter, failure_metrics, EgoLifeSample, split_by_video, interaction_cases
def test_loader_source_dedup_and_target_is_separate(tmp_path):
 d=tmp_path/'d'; d.mkdir(); (d/'clip.mp4').write_bytes(b'v'); (d/'EgoLife_QA.json').write_text(json.dumps([{'id':'q','video':'clip.mp4','start_frame':1,'end_frame':2,'source':'蓝色杯子在桌上','conversations':[{'value':'杯子在哪里'},{'value':'桌上'}]}],ensure_ascii=False),encoding='utf8'); records,samples,m=load_egolife(d); assert len(records)==1 and samples[0].target['frames']==[1,2] and m['sourceCount']==1
def test_timeline_adapter_metrics(tmp_path):
 d=tmp_path/'d'; d.mkdir(); (d/'clip.mp4').write_bytes(b'v'); (d/'EgoLife_QA.json').write_text(json.dumps([{'id':'q','video':'clip.mp4','source':'杯子在桌上','conversations':[{'value':'杯子在哪里'},{'value':'桌上'}]}],ensure_ascii=False),encoding='utf8'); records,samples,_=load_egolife(d); result=evaluate_adapter(TimelineAdapter(tmp_path/'m.db'),records,samples); assert result['metrics']['questions']==1; assert set(failure_metrics(result['rows']))=={'sourceMiss','invalidCitation','temporalClarification','duplicateSelected'}


def test_video_split_and_controlled_interaction_cases():
    samples=[EgoLifeSample(str(i), f"v{i%3}", f"s{i}", "q", "a", {}, f"source-{i}") for i in range(9)]
    split=split_by_video(samples, seed=4)
    assert not ({s.video_id for s in split["calibration"]} & {s.video_id for s in split["test"]})
    assert {s.video_id for s in split["development"]}
    cases=interaction_cases(split["test"])
    assert cases and cases[0].turns[0].startswith("source-")

from egoassist.eval.egolife_experiments import SourceLedger, classify_failure

def test_source_ledger_is_append_only_and_failure_taxonomy(tmp_path):
    ledger = SourceLedger(tmp_path / "sources.jsonl")
    ledger.append({"sourceId": "s1", "videoId": "v1", "text": "source text"})
    assert ledger.append({"sourceId": "s1", "videoId": "v1", "text": "source text"})["sourceId"] == "s1"
    assert len(ledger.records()) == 1
    try:
        ledger.append({"sourceId": "s1", "videoId": "v1", "text": "tampered"})
        assert False, "conflicting immutable source must be rejected"
    except ValueError:
        pass
    assert ledger.records()[0]["schema"] == "source-event.v1"
    assert classify_failure({"sourceRecall": False, "citationIdsValid": False, "temporalStatus": "needs-clarification"}) == ["retrieval.source-miss", "boundary.invalid-citation", "temporal.unresolved"]

def test_backtest_aggregate_reports_video_strata():
    from egoassist.eval.egolife_experiments import aggregate_backtests
    value=aggregate_backtests([{"metrics":{"sourceRecall":.5,"retrievalMedianMs":2},"rows":[{"videoId":"v","sourceRecall":False,"citationIdsValid":True}]},{"metrics":{"sourceRecall":1,"retrievalMedianMs":4},"rows":[{"videoId":"v","sourceRecall":True,"citationIdsValid":True}]}])
    assert value["sourceRecallMean"] == .75 and value["byVideo"]["v"]["sourceMiss"] == 1

def test_qwen_embedding_partial_cache_preserves_order(tmp_path):
    from egoassist.eval.egolife_experiments import QwenEmbeddingAdapter
    class Resp:
        status_code=200
        output={"embeddings":[{"embedding":[0.,1.] }]}
    class API:
        class TextEmbedding:
            @staticmethod
            def call(**kwargs): return Resp()
    a=QwenEmbeddingAdapter(cache_path=tmp_path/'c.json'); a.client=API(); a.cache={}
    a._encode(['a']); a.cache_hits=0
    v=a._encode(['a','b'])
    assert v.shape == (2,2) and a.cache_hits == 1


def test_partial_provider_failure_does_not_become_quality_zero():
    class Adapter:
        name = "provider"
        def ingest(self, records): pass
        def retrieve(self, query):
            return {"selected": [], "candidates": [], "unavailable": True}
    sample = EgoLifeSample("k", "v", "s", "question", "answer", {}, "source")
    result = evaluate_adapter(Adapter(), [{"sourceId": "s", "videoId": "v", "text": "source"}], [sample])
    assert result["metrics"]["status"] == "unavailable"
    for key in ("sourceRecall", "candidateRecall", "mrr", "sourceRecall@1", "sourceRecall@5", "ndcg@5"):
        assert result["metrics"][key] is None
