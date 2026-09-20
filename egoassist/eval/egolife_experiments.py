"""Source-isolated EgoLife memory benchmark primitives."""
from __future__ import annotations
import hashlib, json, statistics, time, random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol
from egoassist.memory import TimelineMemory
from egoassist.retrieval.index import build_index
import re
import numpy as np
from collections import Counter, defaultdict

@dataclass(frozen=True)
class EgoLifeSample:
    key: str; video_id: str; source_id: str; question: str; expected: str; target: dict[str, Any]; source: str

def _clean(value: str) -> str:
    return str(value).replace("<speech>\n<image>\n", "").replace("<speech>", "").replace("<image>", "").strip()

def load_egolife(root: str | Path, *, max_questions: int | None = None):
    root = Path(root); qa_path = root / "EgoLife_QA.json"; rows = json.loads(qa_path.read_text(encoding="utf-8")); videos = {p.stem for p in root.glob("*.mp4")}
    records, samples, seen = [], [], {}
    for row in rows:
        video = Path(str(row.get("video", ""))).stem; source = row.get("source")
        if video not in videos or not source: continue
        source = str(source); sid = seen.setdefault(source, f"source-{len(seen):05d}")
        if not any(r["sourceId"] == sid for r in records): records.append({"sourceId": sid, "videoId": video, "text": source, "clip": row.get("id"), "frames": [row.get("start_frame"), row.get("end_frame")]})
        conv = row.get("conversations") or []
        if len(conv) >= 2: samples.append(EgoLifeSample(f"{video}:{row.get('id')}", video, sid, _clean(conv[0].get("value", "")), _clean(conv[1].get("value", "")), {"clip": row.get("id"), "frames": [row.get("start_frame"), row.get("end_frame")]}, source))
    samples.sort(key=lambda x: x.key); samples = samples[:max_questions] if max_questions is not None else samples
    return records, samples, {"schema": "egoassist.egolife.v2", "qaSha256": hashlib.sha256(qa_path.read_bytes()).hexdigest(), "videoIds": sorted(videos), "records": len(records), "questions": len(samples), "sourceCount": len(seen), "claimBoundary": "Target frames are never supplied to retrieval adapters."}

def load_egolife_tree(root: str | Path, *, max_questions: int | None = None):
    """Merge all EgoLife day/person folders with globally unique source IDs."""
    root=Path(root); all_records=[]; all_samples=[]; manifests=[]
    for qa in sorted(root.rglob("EgoLife_QA.json")):
        records, samples, manifest = load_egolife(qa.parent)
        prefix=qa.parent.name + ":"
        for record in records:
            record={**record, "sourceId": prefix + record["sourceId"], "videoId": prefix + record["videoId"]}; all_records.append(record)
        for sample in samples:
            all_samples.append(EgoLifeSample(prefix+sample.key, prefix+sample.video_id, prefix+sample.source_id, sample.question, sample.expected, sample.target, sample.source))
        manifests.append(manifest)
    all_samples.sort(key=lambda x:x.key)
    if max_questions is not None: all_samples=all_samples[:max_questions]
    digest=hashlib.sha256("".join(m["qaSha256"] for m in manifests).encode()).hexdigest()
    return all_records, all_samples, {"schema":"egoassist.egolife.tree.v1", "qaSha256":digest, "folders":len(manifests), "records":len(all_records), "questions":len(all_samples), "sourceCount":len(all_records)}

def split_by_video(samples: list[EgoLifeSample], *, seed: int = 0,
                   calibration_ratio: float = .2, test_ratio: float = .2):
    """Deterministically split by video into development, regression and freeze sets."""
    if not 0 <= calibration_ratio < 1 or not 0 <= test_ratio < 1 or calibration_ratio + test_ratio >= 1:
        raise ValueError("calibration_ratio and test_ratio must leave development videos")
    videos = sorted({s.video_id for s in samples}); random.Random(seed).shuffle(videos)
    if len(videos) < 3:
        return {"development": samples, "calibration": [], "test": [], "status": "no-held-out-video"}
    n_cal = max(1, round(len(videos) * calibration_ratio))
    n_test = max(1, round(len(videos) * test_ratio))
    if n_cal + n_test >= len(videos):
        n_cal, n_test = 1, 1
    cal=set(videos[:n_cal]); test=set(videos[n_cal:n_cal+n_test]); dev=set(videos[n_cal+n_test:])
    return {"development": [s for s in samples if s.video_id in dev],
            "calibration": [s for s in samples if s.video_id in cal],
            "test": [s for s in samples if s.video_id in test], "status": "held-out-video"}

@dataclass(frozen=True)
class InteractionCase:
    case_id: str; turns: tuple[str, ...]; expected: tuple[str, ...]

def interaction_cases(samples: list[EgoLifeSample], *, limit: int | None = None):
    cases=[]
    for s in samples:
        cases.append(InteractionCase(s.key, (s.source, s.question, "请只根据刚才那段历史回答，并说明日期。", "我说的是今天，不是之前。"), ("write-source", "retrieve", "date-grounding", "correction")))
        if limit and len(cases) >= limit: break
    return cases

class MemoryAdapter(Protocol):
    name: str
    def ingest(self, records: Iterable[dict[str, Any]]) -> None: ...
    def retrieve(self, sample: RetrievalQuery) -> dict[str, Any]: ...

class TimelineAdapter:
    name = "timeline-structured-lexical"
    def __init__(self, path): self.memory = TimelineMemory(path)
    def ingest(self, records):
        self.records = {r["sourceId"]: r for r in records}
        for r in records: self.memory.remember(r["text"], user_id="egolife", case_id="dataset", scope="case", kind="event", source_ids=[r["sourceId"]], card_id=r["sourceId"], metadata={"egolife": True, "videoId": r["videoId"]})
    def retrieve(self, sample):
        # The default Timeline selection is the baseline; audit rows are not an
        # eligibility pool and may include hard-rejected sources.
        result = self.memory.retrieve(sample.question, user_id="egolife", case_id="dataset",
                                      scope="case", limit=5, include_hard_candidates=True)
        return {**result, "auditCandidates": result["candidates"],
                "candidates": result["hardCandidates"]}

def _hashed_vector(text: str, dim: int = 256) -> np.ndarray:
    """Deterministic local hashed-token vector; a reproducible lexical baseline, not an embedding model."""
    v = np.zeros(dim, dtype=np.float32)
    for tok in re.findall(r"[\\w]+", text.lower()):
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little") % dim
        v[h] += 1.0
    n = np.linalg.norm(v)
    return v / n if n else v

class VectorAdapter:
    name = "deterministic-hashed-vector"
    def __init__(self, path=None, *, dim: int = 256, top_k: int = 5):
        self.dim, self.top_k, self.records = dim, top_k, []
        self.index = build_index(dim, force_flat=True)
    def ingest(self, records):
        self.records = list(records)
        for i, r in enumerate(self.records): self.index.add(i, _hashed_vector(r["text"], self.dim))
    def retrieve(self, sample):
        hits = self.index.search(_hashed_vector(sample.question, self.dim), self.top_k)
        selected = [{"cardId": self.records[i]["sourceId"], "score": s} for i,s in hits]
        return {"selected": selected, "candidates": selected, "routing": {"temporalStatus": "unknown", "referenceStatus": "unknown"}, "adapterNote": "hashed-vector-baseline"}

class SemanticEmbeddingAdapter:
    """Optional sentence-transformers route; fails closed when unavailable."""
    name = "semantic-embedding"
    def __init__(self, *args, model_name="all-MiniLM-L6-v2", **kwargs):
        import os
        if os.getenv("EGOASSIST_ENABLE_LOCAL_SEMANTIC", "0").lower() not in {"1", "true", "yes"}:
            self.error = "disabled-by-default"; self.model = None
            return
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            self.error = str(exc); self.model = None
            return
        self.model = SentenceTransformer(model_name); self.records=[]; self.index=None
    def ingest(self, records):
        if self.model is None: return
        self.records=list(records); vec=self.model.encode([r['text'] for r in self.records], normalize_embeddings=True)
        self.index=build_index(int(vec.shape[1]), force_flat=True)
        for i,v in enumerate(vec): self.index.add(i,v)
    def retrieve(self, sample):
        if self.model is None:
            return {"selected": [], "candidates": [], "routing": {"temporalStatus": "unknown"}, "unavailable": True, "reason": self.error}
        q=self.model.encode([sample.question], normalize_embeddings=True)[0]
        hits=self.index.search(q,5); selected=[{"cardId":self.records[i]['sourceId'],"score":s} for i,s in hits]
        return {"selected":selected,"candidates":selected,"routing":{"temporalStatus":"unknown"},"adapterNote":"sentence-transformers"}

class QwenEmbeddingAdapter:
    """DashScope Qwen text-embedding route, isolated from production storage."""
    name = "qwen-text-embedding-v3"
    def __init__(self, *, model="text-embedding-v3", cache_path=None):
        self.model_name=model; self.records=[]; self.index=None; self.error=None
        self.cache_path=Path(cache_path) if cache_path else None; self.cache={}; self.api_calls=0; self.cache_hits=0
        if self.cache_path and self.cache_path.exists():
            try: self.cache=json.loads(self.cache_path.read_text(encoding="utf8"))
            except Exception: self.cache={}
        try:
            import dashscope
            import os
            try:
                from dotenv import load_dotenv
                load_dotenv()
            except Exception:
                pass
            key=os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
            if not key: raise RuntimeError("embedding API key unavailable")
            dashscope.api_key=key; self.client=dashscope
        except Exception as exc: self.client=None; self.error=str(exc)
    def _encode(self, texts):
        keys=[hashlib.sha256((self.model_name+"\0"+t).encode()).hexdigest() for t in texts]
        found=[self.cache.get(k) for k in keys]
        if all(x is not None for x in found): self.cache_hits += len(texts); return np.asarray(found,dtype=np.float32)
        missing=[(i,t) for i,(t,v) in enumerate(zip(texts,found)) if v is None]
        out={}
        self.cache_hits += len(texts)-len(missing)
        for start in range(0, len(missing), 10):
            batch=[t for _,t in missing[start:start+10]]; resp=self.client.TextEmbedding.call(model=self.model_name, input=batch); self.api_calls += 1
            if getattr(resp,"status_code",200)!=200: raise RuntimeError(str(resp))
            data=resp.output.get("embeddings", resp.output.get("embedding", []))
            for (idx,_), value in zip(missing[start:start+10], data): out[idx]=value.get("embedding",value)
        vectors=[]
        for i,k in enumerate(keys):
            value=found[i] if found[i] is not None else out[i]
            self.cache[k]=value; vectors.append(value)
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True,exist_ok=True); self.cache_path.write_text(json.dumps(self.cache),encoding="utf8")
        return np.asarray(vectors, dtype=np.float32)
    def ingest(self, records):
        if self.client is None: return
        try:
            self.records=list(records); vec=self._encode([r['text'] for r in self.records]); self.index=build_index(vec.shape[1], force_flat=True)
            for i,v in enumerate(vec): self.index.add(i,v/(np.linalg.norm(v) or 1))
        except Exception as exc: self.error=str(exc); self.client=None
    def retrieve(self, sample):
        if self.client is None or self.index is None: return {"selected":[],"candidates":[],"routing":{},"unavailable":True,"reason":self.error}
        try:
            q=self._encode([sample.question])[0]; q=q/(np.linalg.norm(q) or 1); hits=self.index.search(q,5)
            sel=[{"cardId":self.records[i]['sourceId'],"score":s} for i,s in hits]; return {"selected":sel,"candidates":sel,"routing":{},"adapterNote":"qwen-api"}
        except Exception as exc: return {"selected":[],"candidates":[],"routing":{},"unavailable":True,"reason":str(exc)}

class HybridAdapter:
    name = "timeline-hybrid-hashed-vector"
    def __init__(self, path=None, *, dim: int = 256, top_k: int = 5):
        self.timeline = TimelineAdapter(path or ":memory:"); self.dim, self.top_k = dim, top_k; self.records=[]; self.vindex=build_index(dim, force_flat=True)
    def ingest(self, records):
        self.records=list(records); self.timeline.ingest(records)
        for i,r in enumerate(self.records): self.vindex.add(i, _hashed_vector(r["text"], self.dim))
    def retrieve(self, sample):
        # Timeline performs temporal/identity/source hard gates; vector only reranks eligible candidates.
        base=self.timeline.memory.retrieve(sample.question, user_id="egolife", case_id="dataset", scope="case", limit=max(50,self.top_k), include_hard_candidates=True)
        allowed={x.get("cardId") or x.get("episodeId") for x in base.get("hardCandidates", [])}
        vh=self.vindex.search(_hashed_vector(sample.question,self.dim), max(50,self.top_k)); score={self.records[i]["sourceId"]:s for i,s in vh}
        # Re-rank the complete hard-gated candidate pool; limiting to Timeline's
        # lexical top-k would preserve the observed truncation failure.
        pool=[x for x in base.get("hardCandidates",[]) if (x.get("cardId") or x.get("episodeId")) in allowed]
        # Reciprocal-rank fusion prevents a weak local vector baseline from
        # discarding useful lexical evidence while still recovering candidates
        # that lexical top-k would truncate.
        lexical_rank={((x.get("cardId") or x.get("episodeId"))): i+1 for i,x in enumerate(base.get("hardCandidates", []))}
        semantic_rank={sid: i+1 for i, sid in enumerate(sorted(allowed, key=lambda sid: (-score.get(sid, 0.0), sid)))}
        def fused(x):
            sid=x.get("cardId") or x.get("episodeId")
            return 1.0/(60+lexical_rank.get(sid, 999)) + 1.0/(60+semantic_rank[sid])
        selected=sorted(pool, key=fused, reverse=True)[:self.top_k]
        return {**base, "selected": selected, "candidates": pool, "adapterNote": "pure-rrf-after-hard-gates"}

@dataclass(frozen=True)
class RetrievalQuery:
    """The only per-question input allowed across the retrieval boundary."""
    question: str


def evaluate_adapter(adapter, records, samples, *, seed=0):
    adapter.ingest([{k: r[k] for k in ("sourceId", "videoId", "text")} for r in records]); rows=[]; lat=[]; ids={r["sourceId"] for r in records}
    for s in samples:
        start=time.perf_counter(); result=adapter.retrieve(RetrievalQuery(s.question)); elapsed=(time.perf_counter()-start)*1000; selected=result.get("selected", []); found=[x.get("cardId") or x.get("episodeId") for x in selected]; lat.append(elapsed)
        candidate_ids=[x.get("cardId") or x.get("episodeId") for x in result.get("candidates", [])]
        rows.append({"key": s.key, "videoId": s.video_id, "goldSourceId": s.source_id, "selectedIds": found, "candidateIds": candidate_ids, "sourceRecall": s.source_id in found, "sourceRecallCandidate": s.source_id in candidate_ids, "citationIdsValid": all(x in ids for x in found), "temporalStatus": result.get("routing", {}).get("temporalStatus"), "candidateCount": len(candidate_ids), "retrievalMs": round(elapsed, 3), "unavailable": result.get("unavailable", False), "unavailableReason": result.get("reason")})
    unavailable = any(r["unavailable"] for r in rows) or bool(adapter.name in {"semantic-embedding", "qwen-text-embedding-v3"} and (getattr(adapter, "model", True) is None or getattr(adapter, "client", True) is None))
    metrics={"questions": len(rows), "sourceRecall": None if unavailable else (sum(r["sourceRecall"] for r in rows)/len(rows) if rows else None), "candidateRecall": None if unavailable else (sum(r["sourceRecallCandidate"] for r in rows)/len(rows) if rows else None), "citationValidity": None if unavailable else (sum(r["citationIdsValid"] for r in rows)/len(rows) if rows else None), "retrievalMedianMs": statistics.median(lat) if lat else None, "apiCalls": getattr(adapter,"api_calls",None), "cacheHits": getattr(adapter,"cache_hits",None), "status": "unavailable" if unavailable else "measured"}
    metrics.update(dict.fromkeys(ranking_metrics([])) if unavailable else ranking_metrics(rows))
    return {"adapter": adapter.name, "seed": seed, "rows": rows, "metrics": metrics}

def failure_metrics(rows):
    rows=list(rows); return {"sourceMiss": sum(not r["sourceRecall"] for r in rows), "invalidCitation": sum(not r["citationIdsValid"] for r in rows), "temporalClarification": sum(r.get("temporalStatus")=="needs-clarification" for r in rows), "duplicateSelected": sum(len(r["selectedIds"]) != len(set(r["selectedIds"])) for r in rows)}

def aggregate_backtests(runs):
    """Aggregate independent seeds without treating questions from one video as independent."""
    runs=list(runs); values=[r.get("metrics",{}).get("sourceRecall") for r in runs if r.get("metrics",{}).get("sourceRecall") is not None]
    lat=[r.get("metrics",{}).get("retrievalMedianMs") for r in runs if r.get("metrics",{}).get("retrievalMedianMs") is not None]
    by_video={}
    for run in runs:
        for row in run.get("rows",[]):
            slot=by_video.setdefault(row["videoId"], {"questions":0,"sourceMiss":0,"invalidCitation":0})
            slot["questions"]+=1; slot["sourceMiss"]+=int(not row["sourceRecall"]); slot["invalidCitation"]+=int(not row["citationIdsValid"])
    return {"seeds":len(runs), "sourceRecallMean":sum(values)/len(values) if values else None,
            "sourceRecallMin":min(values) if values else None, "sourceRecallMax":max(values) if values else None,
            "retrievalMedianMsMean":sum(lat)/len(lat) if lat else None, "byVideo":by_video,
            "claimBoundary":"Aggregated per-seed adapter measurements; no answer accuracy or calibrated probability."}

def ranking_metrics(rows, k_values=(1, 5)):
    """Compute source ranking metrics from evaluate_adapter rows.

    Missing gold IDs are treated as unmeasured (``None``), never as zero.
    """
    rows=list(rows); out={}
    ranks=[]
    for row in rows:
        gold=row.get("goldSourceId"); ids=list(row.get("selectedIds", row.get("candidateIds", [])) or [])
        if not gold: continue
        try: rank=ids.index(gold)+1
        except ValueError: rank=None
        ranks.append(rank)
    out["mrr"] = (sum(1/r for r in ranks if r)/len(ranks)) if ranks else None
    for k in k_values:
        vals=[1.0 if r and r<=k else 0.0 for r in ranks]
        out[f"sourceRecall@{k}"]=sum(vals)/len(vals) if vals else None
        # one relevant item per query; DCG reduces to reciprocal log rank
        gains=[1/np.log2(r+1) if r and r<=k else 0.0 for r in ranks]
        out[f"ndcg@{k}"]=sum(gains)/len(gains) if gains else None
    return out

class QwenJudgeAdapter:
    """Independent Qwen LLM-as-judge adapter.

    A callable client may be injected for tests/offline runs. The adapter never
    receives gold answers and records raw request/response metadata.
    """
    SCHEMA=("evidence_support","temporal_correctness","subject_correctness",
            "reference_resolution","completeness","clarification_quality",
            "contradiction_handling","citation_validity","overall")
    def __init__(self, client=None, *, model="qwen-plus", price_per_1k_tokens=None):
        self.client=client; self.model=model; self.price_per_1k_tokens=price_per_1k_tokens
    def judge(self, question, answer, citations, context=None):
        payload={"question":question,"answer":answer,"citations":citations,"context":context or {}}
        started=time.perf_counter()
        if self.client is None:
            return {"status":"unavailable","model":self.model,"request":payload,"scores":None,
                    "latencyMs":round((time.perf_counter()-started)*1000,3)}
        raw=self.client(payload)
        if isinstance(raw,str): raw=json.loads(raw)
        scores={k:raw.get(k) for k in self.SCHEMA if k in raw}
        return {"status":"measured","model":self.model,"request":payload,"response":raw,
                "scores":scores,"rationale":raw.get("rationale"),"errorTags":raw.get("error_tags",[]),
                "judgeConfidence":raw.get("judge_confidence"),"latencyMs":round((time.perf_counter()-started)*1000,3),
                "tokenUsage":raw.get("usage"),"cost":raw.get("cost")}

def cluster_failures(rows):
    """Group row failure tags into reproducible clusters with sample keys."""
    clusters=defaultdict(list)
    for row in rows:
        tags=classify_failure(row) + list(row.get("errorTags") or row.get("error_tags") or [])
        for tag in sorted(set(tags)): clusters[tag].append(row.get("key"))
    return [{"tag":tag,"count":len(keys),"sampleKeys":keys[:20]} for tag,keys in sorted(clusters.items(), key=lambda kv:(-len(kv[1]),kv[0]))]

class SourceLedger:
    """Append-only source events shared by all experiment adapters."""
    def __init__(self, path): self.path=Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
    def append(self, record):
        if not {"sourceId", "videoId", "text"} <= set(record): raise ValueError("source event requires sourceId, videoId and text")
        row={**record, "schema":"source-event.v1"}
        # Source ingestion is idempotent across retries and concurrent writers.
        # A reused source id with different payload is rejected as corruption.
        existing = {item.get("sourceId"): item for item in self.records()}
        prior = existing.get(str(row["sourceId"]))
        if prior is not None:
            if (prior.get("videoId"), prior.get("text")) != (row.get("videoId"), row.get("text")):
                raise ValueError("sourceId already exists with different immutable payload")
            return prior
        with self.path.open("a", encoding="utf8") as handle: handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True)+"\n")
        return row
    def records(self): return [json.loads(x) for x in self.path.read_text(encoding="utf8").splitlines() if x.strip()] if self.path.exists() else []

def classify_failure(row):
    out=[]
    if not row.get("sourceRecall"): out.append("retrieval.source-miss")
    if not row.get("citationIdsValid", True): out.append("boundary.invalid-citation")
    if row.get("temporalStatus")=="needs-clarification": out.append("temporal.unresolved")
    if row.get("duplicateSelected"): out.append("write.duplicate")
    if row.get("crossCaseLeakage"): out.append("boundary.cross-case-leakage")
    return out
