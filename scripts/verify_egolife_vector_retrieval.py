"""Verify vector experiment provenance, cache coverage and selection boundaries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import numpy as np


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify(dataset, previous, output):
    root = Path(__file__).resolve().parents[1]
    plan = read(output / "plan.json")
    for name, digest in plan["codeSha256"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    for name, key in [("sources.json", "sourceSha256"), ("questions.json", "questionSha256")]:
        assert hashlib.sha256((dataset / name).read_bytes()).hexdigest() == plan[key]
    queries = {r["id"]: r for r in read(dataset / "questions.json")}
    sources = {r["sourceId"]: r for r in read(dataset / "sources.json")}
    old = {r["id"]: r for r in read(previous / "retrieval.json")}
    rows = read(output / "predictions.json")
    lookup = {(r["id"], r["seed"], r["arm"]): r for r in rows}
    assert len(rows) == len(lookup) == len(queries) * len(plan["seeds"]) * len(plan["arms"])
    for row in rows:
        pool, selected = set(row["candidateIds"]), row["selectedIds"]
        assert len(selected) <= 5 and len(selected) == len(set(selected))
        assert set(selected).issubset(pool)
        assert all(sources[sid]["end"] <= queries[row["id"]]["cutoff"] for sid in pool)
        same = lookup[row["id"], row["seed"], "timeline"]
        if row["arm"] == "timeline":
            assert selected == old[row["id"]]["selectedIds"]
            assert pool == set(old[row["id"]]["candidateIds"])
        elif row["arm"] == "vector-same-pool":
            assert pool == set(same["candidateIds"])
        elif row["arm"] == "vector-full":
            assert pool.issubset(row["denseNominatedIds"])
            assert len(row["denseNominatedIds"]) == plan["budget"]
            assert all(sources[sid]["end"] <= queries[row["id"]]["cutoff"] for sid in row["denseNominatedIds"])
        elif row["arm"] == "rrf-union":
            dense = lookup[row["id"], row["seed"], "vector-full"]
            assert pool == set(same["candidateIds"]) | set(dense["candidateIds"])
        first = lookup[row["id"], plan["seeds"][0], row["arm"]]
        assert row["selectedIds"] == first["selectedIds"]
        assert row["candidateIds"] == first["candidateIds"]
    cache_path = Path(plan.get("embeddingCache", output / "embedding-cache.sqlite3"))
    db = sqlite3.connect(f"file:{cache_path.as_posix()}?mode=ro", uri=True)
    keys = set()
    for key, raw in db.execute("SELECT key, vector FROM vectors"):
        vector = np.frombuffer(raw, dtype=np.float32)
        assert vector.shape == (plan["dimension"],)
        assert np.isfinite(vector).all() and abs(float(np.linalg.norm(vector)) - 1) < 1e-5
        keys.add(key)
    documents = [s["text"] for s in sources.values() if s["end"] <= max(q["cutoff"] for q in queries.values())]
    expected = set()
    for role, texts in [("document", documents), ("query", [q["question"] for q in queries.values()])]:
        for text in texts:
            key = hashlib.sha256(json.dumps([plan["model"], plan["dimension"], role, text], ensure_ascii=False).encode()).hexdigest()
            expected.add(key)
    assert keys == expected
    # Recheck the actual nominations against the shared gate implementation.
    # Only nominated IDs are scanned, all of which already passed the cutoff
    # assertions above; the original database remains read-only.
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "scripts"))
    from egoassist.memory import TimelineMemory
    from egolife_independent_modes import synthetic_time
    original = sqlite3.connect(f"file:{(previous / 'timeline.sqlite3').as_posix()}?mode=ro", uri=True)
    verification_path = output / "verification-timeline.sqlite3"
    copied = sqlite3.connect(verification_path)
    original.backup(copied)
    original.close()
    copied.close()
    memory = TimelineMemory(verification_path)
    os.environ["EGOASSIST_MEMORY_CANDIDATE_LIMIT"] = str(plan["budget"])
    for qid, query in queries.items():
        row = lookup[qid, plan["seeds"][0], "vector-full"]
        result = memory.retrieve(query["question"], user_id="A1_JAKE", case_id="official-heldout", scope="case",
            now=synthetic_time(query["cutoff"]), candidate_card_ids=row["denseNominatedIds"], include_hard_candidates=True)
        assert {r["cardId"] for r in result["hardCandidates"]} == set(row["candidateIds"])
        for candidate in result["hardCandidates"]:
            assert candidate["card"]["text"] == sources[candidate["cardId"]]["text"].strip()
            assert candidate["card"]["sourceIds"] == [candidate["cardId"]]
    result = {"passed": True, "predictionRows": len(rows), "uniqueQuestions": len(queries),
              "uniqueCachedEmbeddings": len(keys), "baselineReproduced": True,
              "samePoolAndUnionBoundaries": True, "noFutureSources": True,
              "seedSelectionsIdentical": True, "frozenCodeAndDataUnchanged": True,
              "cacheShapeNormAndCoverage": True, "actualVectorGatesRechecked": len(queries),
              "hardCandidateTextsAndSourceIdsUnchanged": True}
    (output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.dataset, args.previous, args.output)
