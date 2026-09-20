"""Attribute retrieval loss on already exposed regression questions.

Never edits source artifacts or tunes production limits. Gold is evaluated only
after each question-only retrieval finishes. Point-time overlap is a diagnostic proxy.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egoassist.memory import TimelineMemory
from egoassist.eval.controlled_retrieval import BM25, ranking
from egolife_independent_modes import absolute_seconds, clock_seconds, synthetic_time


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def diagnose(args):
    args.output.mkdir(parents=True, exist_ok=False)
    plan = {"evaluationRole": "exposed-regression-diagnostic", "independentTest": False,
            "budgets": [256, 1024, 4096], "productionDefaultChanged": False,
            "rankingComparison": "existing rank vs bigram BM25 rerank of the identical hard pool",
            "goldUse": "post-retrieval timestamp overlap attribution only; not passed to rankers",
            "qaSha256": hashlib.sha256((args.dataset / "questions.json").read_bytes()).hexdigest(),
            "sourceDatabase": str(args.previous / "timeline.sqlite3"), "modelCalls": 0}
    save(args.output / "plan.json", plan)
    original = sqlite3.connect(f"file:{(args.previous / 'timeline.sqlite3').as_posix()}?mode=ro", uri=True)
    copied = sqlite3.connect(args.output / "diagnostic.sqlite3")
    original.backup(copied)
    original.close()
    copied.close()
    memory = TimelineMemory(args.output / "diagnostic.sqlite3")
    queries = sorted(read(args.dataset / "questions.json"), key=lambda q: q["cutoff"], reverse=True)
    sources = {r["sourceId"]: r for r in read(args.dataset / "sources.json")}
    gold = {r["id"]: r for r in read(args.dataset / "gold-private.json")}
    old = {r["id"]: r for r in read(args.previous / "retrieval.json")}
    rows = []
    previous = os.environ.get("EGOASSIST_MEMORY_CANDIDATE_LIMIT")
    try:
        for index, query in enumerate(queries, 1):
            with memory._connect() as db:
                db.execute("DELETE FROM memory_cards WHERE recorded_at>?", (synthetic_time(query["cutoff"]).isoformat(timespec="milliseconds"),))
            for budget in plan["budgets"]:
                os.environ["EGOASSIST_MEMORY_CANDIDATE_LIMIT"] = str(budget)
                started = time.perf_counter()
                result = memory.retrieve(query["question"], user_id="A1_JAKE", case_id="official-heldout", scope="case",
                                         limit=5, now=synthetic_time(query["cutoff"]), include_hard_candidates=True,
                                         include_candidate_audit=True)
                elapsed = (time.perf_counter() - started) * 1000
                audit, hard = result["candidateAudit"], result["hardCandidates"]
                selected = [r["cardId"] for r in result["selected"]]
                hard_ids = {r["cardId"] for r in hard}
                assert set(selected).issubset(hard_ids)
                assert all(sources[r["cardId"]]["end"] <= query["cutoff"] for r in audit)
                texts = {r["cardId"]: r["card"]["text"] for r in hard}
                ranker = BM25(texts, chars=True)
                reranked = ranking(ranker.scores(query["question"], texts))[:5]
                assert set(reranked).issubset(hard_ids)
                # Evaluation begins only after both rankings are fixed.
                target = gold[query["id"]]["target"]
                moment = absolute_seconds(target["date"], clock_seconds(target["time"]))
                targets = {sid for sid, r in sources.items() if r["kind"] == "caption" and
                           r["start"] < moment + 1 and r["end"] >= moment and r["end"] <= query["cutoff"]}
                scanned = {r["cardId"] for r in audit}
                stage = ("target-not-available" if not targets else "candidate-generation" if not targets & scanned else
                         "hard-gate" if not targets & hard_ids else "ranking-or-threshold" if not targets & set(selected) else "selected")
                target_audit = [{"sourceId": r["cardId"], "score": r["score"], "hardEligible": r["hardEligible"],
                                 "rejections": r["rejectionReasons"], "rank": n + 1}
                                for n, r in enumerate(audit) if r["cardId"] in targets]
                row = {"id": query["id"], "budget": budget, "lossStage": stage, "latencyMs": elapsed,
                       "scanned": len(audit), "hardEligible": len(hard), "selected": len(selected),
                       "targetAvailable": bool(targets), "targetInScanned": bool(targets & scanned),
                       "targetInHard": bool(targets & hard_ids), "targetInSelected": bool(targets & set(selected)),
                       "targetInBigramRerank": bool(targets & set(reranked)), "targetAudit": target_audit,
                       "gateRejections": dict(Counter(reason for r in audit if not r["hardEligible"] for reason in r["rejectionReasons"])),
                       "topScoreOne": sum(r["score"] == 1 for r in hard), "minimumScore": result["minimumScore"],
                       "queryTokensUnderTrigramLength": sum(len(t) < 3 for t in memory._tokenize(query["question"])),
                       "queryTokens": len(memory._tokenize(query["question"])),
                       "baselineSelectionMatches": selected == old[query["id"]]["selectedIds"] if budget == 256 else None}
                rows.append(row)
                save(args.output / "results.json", rows)
            print(json.dumps({"questions": index, "total": len(queries), "runs": len(rows)}), flush=True)
    finally:
        if previous is None:
            os.environ.pop("EGOASSIST_MEMORY_CANDIDATE_LIMIT", None)
        else:
            os.environ["EGOASSIST_MEMORY_CANDIDATE_LIMIT"] = previous
    summary = {"independentTest": False, "proxy": "gold target-time overlap, not full answer evidence recall", "budgets": {}}
    for budget in plan["budgets"]:
        subset = [r for r in rows if r["budget"] == budget]
        summary["budgets"][str(budget)] = {"questions": len(subset), "lossStages": dict(Counter(r["lossStage"] for r in subset)),
            "medianMs": statistics.median(r["latencyMs"] for r in subset),
            **{field: sum(r[field] for r in subset) for field in ("targetAvailable", "targetInScanned", "targetInHard", "targetInSelected", "targetInBigramRerank")},
            "queriesWithSaturatedScores": sum(r["topScoreOne"] > 5 for r in subset),
            "gateRejections": dict(sum((Counter(r["gateRejections"]) for r in subset), Counter()))}
    summary["baselineSelectionsReproduced"] = all(r["baselineSelectionMatches"] for r in rows if r["budget"] == 256)
    save(args.output / "summary.json", summary)
    assert summary["baselineSelectionsReproduced"], "Baseline mismatch: do not interpret diagnostic comparisons"
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    diagnose(parser.parse_args())
