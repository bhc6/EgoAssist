"""Validate frozen inputs, paired completeness, eligibility and recorded scores."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify(dataset, output):
    freeze = read(dataset / "freeze.json")
    for name, digest in freeze["files"].items():
        assert hashlib.sha256((dataset / name).read_bytes()).hexdigest() == digest, name
    questions = {r["id"]: r for r in read(dataset / "questions.json")}
    sources = {r["sourceId"]: r for r in read(dataset / "sources.json")}
    retrieval = {r["id"]: r for r in read(output / "retrieval.json")}
    gold = {r["id"]: str(r["answer"]).strip().upper() for r in read(dataset / "gold-private.json")}
    predictions = read(output / "predictions.json")
    scored = read(output / "scored.json")
    assert set(questions) == set(retrieval) == set(gold)
    expected = {(qid, seed, backend) for qid in questions for seed in (17, 43, 91) for backend in ("omni", "modular")}
    counts = Counter((r["id"], r["seed"], r["backend"]) for r in predictions)
    assert set(counts) == expected and set(counts.values()) == {1}
    for qid, row in retrieval.items():
        assert row["query"] == questions[qid]
        assert set(row["selectedIds"]).issubset(row["candidateIds"])
        assert all(sources[sid]["end"] <= questions[qid]["cutoff"] for sid in row["candidateIds"])
        assert [r["sourceId"] for r in row["evidence"]] == row["selectedIds"]
        assert all(r["text"] == sources[r["sourceId"]]["text"] for r in row["evidence"])
    for raw, row in zip(predictions, scored, strict=True):
        assert all(row[k] == value for k, value in raw.items())
        assert row["correct"] == (row["choice"] == gold[row["id"]])
    report = {"passed": True, "questions": len(questions), "predictions": len(predictions),
              "freezeUnchanged": True, "pairsComplete": True, "noFutureCandidates": True,
              "selectedSubsetOfHardCandidates": True, "evidenceTextExact": True, "scoringPreservesPredictions": True,
              "uniqueQuestionsNotRepeatedSamples": len(questions),
              "retrieval": {"noEvidenceQuestions": sum(not r["evidence"] for r in retrieval.values()),
                            "truncatedQueries": sum(r["scan"]["candidatesTruncated"] for r in retrieval.values()),
                            "medianMs": statistics.median(r["retrievalMs"] for r in retrieval.values()),
                            "ingestSeconds": sum(r["newIngestMs"] for r in retrieval.values()) / 1000},
              "byQueryDay": {}}
    for day in sorted({q["day"] for q in questions.values()}):
        report["byQueryDay"][day] = {}
        for backend in ("omni", "modular"):
            selected = [r for r in scored if r["backend"] == backend and questions[r["id"]]["day"] == day]
            report["byQueryDay"][day][backend] = {"correct": sum(r["correct"] for r in selected), "requests": len(selected),
                                                  "answered": sum(r["choice"] in {"A", "B", "C", "D"} for r in selected)}
    (output / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.dataset, args.output)
