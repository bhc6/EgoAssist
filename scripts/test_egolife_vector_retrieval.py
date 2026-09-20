"""Real Qwen vector retrieval on exposed EgoLife questions, with shared hard gates."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import time

import numpy as np
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist.memory import TimelineMemory
from egoassist.eval.controlled_retrieval import rrf, ranking
from egolife_independent_modes import absolute_seconds, clock_seconds, synthetic_time

MODEL = "text-embedding-v3"
DIM = 1024
SEEDS = [17, 43, 91]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rewind_to_cutoff(memory, cutoff):
    with memory._connect() as db:
        db.execute("DELETE FROM memory_cards WHERE recorded_at>?",
                   (synthetic_time(cutoff).isoformat(timespec="milliseconds"),))


class Embeddings:
    """Bounded parallel requests with incremental SQLite cache and usage audit."""
    def __init__(self, path, workers):
        load_dotenv(ROOT / ".env")
        import dashscope
        self.key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        self.url = dashscope.base_http_api_url.rstrip("/") + "/services/embeddings/text-embedding/text-embedding"
        self.workers = workers
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, audit TEXT NOT NULL)")
        self.hits = Counter()

    def fetch(self, batch, role):
        if not self.key:
            raise RuntimeError("Qwen API key unavailable")
        audit = []
        for attempt in range(5):
            started = time.perf_counter()
            row = {"model": MODEL, "role": role, "count": len(batch), "attempt": attempt + 1,
                   "keys": [k for k, _ in batch], "cost": None}
            try:
                response = requests.post(self.url, headers={"Authorization": "Bearer " + self.key},
                    json={"model": MODEL, "input": {"texts": [t for _, t in batch]},
                          "parameters": {"text_type": role, "dimension": DIM}}, timeout=60)
                row["httpStatus"] = response.status_code
                response.raise_for_status()
                data = response.json()
                row["usage"] = data.get("usage")
                indexed = {r["text_index"]: r["embedding"] for r in data["output"]["embeddings"]}
                if set(indexed) != set(range(len(batch))):
                    raise ValueError("incomplete embeddings")
                vec = np.asarray([indexed[i] for i in range(len(batch))], dtype=np.float32)
                norms = np.linalg.norm(vec, axis=1, keepdims=True)
                if vec.shape != (len(batch), DIM) or not np.isfinite(vec).all() or (norms <= 0).any():
                    raise ValueError("invalid embeddings")
                vec /= norms
                row["latencyMs"] = (time.perf_counter() - started) * 1000
                audit.append(row)
                return [(k, v.tobytes()) for (k, _), v in zip(batch, vec)], audit
            except Exception as exc:
                row["error"] = type(exc).__name__
                row["latencyMs"] = (time.perf_counter() - started) * 1000
                audit.append(row)
                if isinstance(exc, (KeyError, ValueError)) or row.get("httpStatus", 500) in (400, 401, 403, 404):
                    break
                time.sleep(min(2 ** attempt, 16))
        return None, audit

    def encode(self, texts, role):
        keys = [hashlib.sha256(json.dumps([MODEL, DIM, role, t], ensure_ascii=False).encode()).hexdigest() for t in texts]
        mapping = dict(zip(keys, texts))
        present = {r[0] for r in self.db.execute("SELECT key FROM vectors")}
        missing = [(k, t) for k, t in mapping.items() if k not in present]
        self.hits[role] += sum(k in present for k in keys)
        print(json.dumps({"embeddingRole": role, "inputs": len(texts), "uniqueMissing": len(missing)}), flush=True)
        batches = [missing[i:i + 10] for i in range(0, len(missing), 10)]
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            for number, (vectors, audit) in enumerate(executor.map(lambda b: self.fetch(b, role), batches), 1):
                with self.db:
                    self.db.executemany("INSERT INTO calls(audit) VALUES (?)", [(json.dumps(a),) for a in audit])
                    if vectors is not None:
                        self.db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)", vectors)
                if vectors is None:
                    raise RuntimeError("Embedding batch failed; saved audit and completed cache entries")
                if number % 50 == 0 or number == len(batches):
                    print(json.dumps({"role": role, "batchesDone": number, "batchesTotal": len(batches)}), flush=True)
        lookup = {k: v for k, v in self.db.execute("SELECT key, vector FROM vectors") if k in mapping}
        return np.stack([np.frombuffer(lookup[k], dtype=np.float32) for k in keys])

    def summary(self):
        rows = [json.loads(r[0]) for r in self.db.execute("SELECT audit FROM calls")]
        tokens = [r.get("usage", {}).get("total_tokens") for r in rows if r.get("usage")]
        return {"model": MODEL, "dimension": DIM, "callsIncludingRetries": len(rows),
                "failedCalls": sum("error" in r for r in rows), "cacheHitsThisExecution": dict(self.hits),
                "reportedTokens": sum(t for t in tokens if t is not None) if tokens else None,
                "requestMedianMs": statistics.median(r["latencyMs"] for r in rows) if rows else None,
                "cost": None}


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    queries = read(args.dataset / "questions.json")
    sources = sorted((s for s in read(args.dataset / "sources.json") if s["end"] <= max(q["cutoff"] for q in queries)),
                     key=lambda s: s["sourceId"])
    plan = {"independentTest": False, "role": "exposed-regression", "preprocessing": "unchanged original SRT cues",
            "model": MODEL, "dimension": DIM, "budget": 256, "seeds": SEEDS,
            "arms": ["timeline", "vector-same-pool", "vector-full", "rrf-union"],
            "seedRole": "order of ranking arms; embeddings and score tie breaks are fixed",
            "sources": len(sources), "questions": len(queries), "goldUse": "post-selection timestamp proxy only",
            "questionSha256": fingerprint(args.dataset / "questions.json"),
            "sourceSha256": fingerprint(args.dataset / "sources.json"),
            "embeddingCache": str(args.embedding_cache or args.output / "embedding-cache.sqlite3"),
            "codeSha256": {str(p.relative_to(ROOT)): fingerprint(p) for p in [Path(__file__).resolve(), ROOT / "egoassist/memory/timeline.py"]}}
    plan_path = args.output / "plan.json"
    if plan_path.exists() and read(plan_path) != plan:
        raise ValueError("Existing run has a different plan; use a new output directory")
    save(plan_path, plan)
    provider = Embeddings(Path(plan["embeddingCache"]), args.workers)
    started = time.perf_counter()
    vectors = provider.encode([s["text"] for s in sources], "document")
    query_vectors = provider.encode([q["question"] for q in queries], "query")
    save(args.output / "embedding-summary.json", {**provider.summary(), "wallSecondsThisExecution": time.perf_counter() - started})
    source_ids = [s["sourceId"] for s in sources]
    source_map = {s["sourceId"]: s for s in sources}
    ends = np.asarray([s["end"] for s in sources])
    index_by_id = {sid: i for i, sid in enumerate(source_ids)}
    previous = {r["id"]: r for r in read(args.previous / "retrieval.json")}
    rows = []
    os.environ["EGOASSIST_MEMORY_CANDIDATE_LIMIT"] = "256"
    for seed in SEEDS:
        db_path = args.output / f"timeline-{seed}.sqlite3"
        original = sqlite3.connect(f"file:{(args.previous / 'timeline.sqlite3').as_posix()}?mode=ro", uri=True)
        copied = sqlite3.connect(db_path)
        original.backup(copied)
        original.close()
        copied.close()
        memory = TimelineMemory(db_path)
        rng = random.Random(seed)
        for qi in sorted(range(len(queries)), key=lambda i: queries[i]["cutoff"], reverse=True):
            query = queries[qi]
            rewind_to_cutoff(memory, query["cutoff"])
            kwargs = dict(user_id="A1_JAKE", case_id="official-heldout", scope="case", limit=5,
                          now=synthetic_time(query["cutoff"]), include_hard_candidates=True)
            started = time.perf_counter()
            scores = vectors @ query_vectors[qi]
            # Stable ties use source ID order. Future sources cannot compete for budget.
            visible = np.flatnonzero(ends <= query["cutoff"])
            dense_indices = visible[np.argsort(-scores[visible], kind="stable")[:256]]
            nominated = [source_ids[i] for i in dense_indices]
            dense_ms = (time.perf_counter() - started) * 1000
            timings = {}
            results = {}
            order = ["timeline", "vector-full"]
            rng.shuffle(order)
            for arm in order:
                started = time.perf_counter()
                results[arm] = memory.retrieve(query["question"], **kwargs,
                    **({"candidate_card_ids": nominated} if arm == "vector-full" else {}))
                timings[arm] = (time.perf_counter() - started) * 1000
            base, dense = results["timeline"], results["vector-full"]
            base_hard = [r["cardId"] for r in base["hardCandidates"]]
            dense_hard = {r["cardId"] for r in dense["hardCandidates"]}
            assert dense_hard.issubset(nominated)
            base_selected = [r["cardId"] for r in base["selected"]]
            assert base_selected == previous[query["id"]]["selectedIds"], "Baseline reproduction failed"
            started = time.perf_counter()
            same_order = sorted(base_hard, key=lambda sid: (-float(scores[index_by_id[sid]]), sid))
            same_ms = (time.perf_counter() - started) * 1000
            dense_order = [sid for sid in nominated if sid in dense_hard]
            started = time.perf_counter()
            union_order = ranking(rrf(base_hard, dense_order))
            fusion_ms = (time.perf_counter() - started) * 1000
            arms = {
                "timeline": (base_hard, base_selected, timings["timeline"]),
                "vector-same-pool": (base_hard, same_order[:5], timings["timeline"] + dense_ms + same_ms),
                "vector-full": (dense_order, dense_order[:5], dense_ms + timings["vector-full"]),
                "rrf-union": (union_order, union_order[:5], dense_ms + timings["timeline"] + timings["vector-full"] + fusion_ms),
            }
            for arm, (pool, selected, latency) in arms.items():
                assert set(selected).issubset(pool)
                assert all(source_map[sid]["end"] <= query["cutoff"] for sid in pool)
                rows.append({"id": query["id"], "seed": seed, "arm": arm, "candidateIds": pool,
                             "selectedIds": selected, "retrievalMs": latency,
                             "group": f"{query['day']}:{query['queryClock'][:2]}",
                             "denseNominatedIds": nominated if arm == "vector-full" else None})
            save(args.output / "predictions.json", rows)
            print(json.dumps({"seed": seed, "question": query["id"], "completedRows": len(rows)}), flush=True)
    # No gold file is loaded until every arm's selection has been persisted.
    gold = {r["id"]: r for r in read(args.dataset / "gold-private.json")}
    qmap = {q["id"]: q for q in queries}
    scored = []
    for row in rows:
        target = gold[row["id"]]["target"]
        moment = absolute_seconds(target["date"], clock_seconds(target["time"]))
        relevant = {s["sourceId"] for s in sources if s["kind"] == "caption" and s["start"] < moment + 1
                    and s["end"] >= moment and s["end"] <= qmap[row["id"]]["cutoff"]}
        rank = next((i + 1 for i, sid in enumerate(row["selectedIds"]) if sid in relevant), None)
        scored.append({**row, "targetAvailable": bool(relevant), "targetInCandidates": bool(relevant & set(row["candidateIds"])),
                       "targetAt1": rank == 1, "targetAt5": rank is not None})
    save(args.output / "scored.json", scored)
    summary = {"independentTest": False, "metric": "target-time caption localization proxy, not standard evidence Recall",
               "embedding": provider.summary(), "answerAccuracy": None, "evidenceSupport": None,
               "baselineReproduced": True, "arms": {}}
    for arm in plan["arms"]:
        subset = [r for r in scored if r["arm"] == arm]
        groups = sorted({r["group"] for r in subset})
        summary["arms"][arm] = {"rows": len(subset), "uniqueQuestions": len(queries),
            "perSeed": {str(seed): {k: sum(r[k] for r in subset if r["seed"] == seed)
                         for k in ["targetAvailable", "targetInCandidates", "targetAt1", "targetAt5"]} for seed in SEEDS},
            "groupMacroTargetAt5": statistics.mean(statistics.mean(r["targetAt5"] for r in subset if r["group"] == g) for g in groups),
            "groups": len(groups), "retrievalMedianMs": statistics.median(r["retrievalMs"] for r in subset)}
    save(args.output / "summary.json", summary)
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--embedding-cache", type=Path)
    run(parser.parse_args())
