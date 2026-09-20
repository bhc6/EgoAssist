"""Read-only independent consistency checks for the A-D multiple-choice run."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def verify(output):
    plan = read(output / "plan.json")
    dataset = Path(plan["dataset"])
    for name, expected in plan["datasetHashes"].items():
        assert hashlib.sha256((dataset / name).read_bytes()).hexdigest() == expected
    queries = {q["id"]: q for q in read(dataset / "questions.json")}
    sources = {s["sourceId"]: s for s in read(dataset / "sources.json")}
    gold = {g["id"]: g for g in read(dataset / "gold-private.json")}
    previous = {r["id"]: r for r in read(Path(plan["previous"]) / "retrieval.json")}
    rows = read(output / "retrieval.json")
    expected = {(qid, seed, arm) for qid in queries for seed in plan["seeds"] for arm in plan["arms"]}
    key = lambda r: (r["id"], r["seed"], r["arm"])
    assert len(rows) == len(expected) and {key(r) for r in rows} == expected
    selected_by_query_arm = {}
    for row in rows:
        assert set(row["candidateIds"]) == set(previous[row["id"]]["candidateIds"])
        assert set(row["rankedIds"]).issubset(row["candidateIds"])
        assert row["selectedIds"] == row["rankedIds"][:5]
        assert all(sources[sid]["end"] <= queries[row["id"]]["cutoff"] for sid in row["candidateIds"])
        pair = (row["id"], row["arm"])
        assert selected_by_query_arm.setdefault(pair, row["selectedIds"]) == row["selectedIds"]
        if row["arm"] == "A":
            assert row["selectedIds"] == previous[row["id"]]["selectedIds"]
        for sid, span in row["provenance"].items():
            assert span["text"] == sources[sid]["text"][span["start"]:span["end"]]
    predictions = read(output / "predictions.json")
    scored = read(output / "scored.json")
    assert len(predictions) == len(scored) == len(expected)
    assert {key(r) for r in predictions} == {key(r) for r in scored} == expected
    indexed = {key(r): r for r in predictions}
    for row in scored:
        pred = indexed[key(row)]
        assert row["choice"] == pred["choice"]
        correct = row["choice"] == gold[row["id"]]["answer"].strip().upper() if pred["status"] == "completed" else None
        assert row["correct"] is correct
        assert 0 <= row["targetNDCG5"] <= 1
    summary = read(output / "summary.json")
    for arm in plan["arms"]:
        group = [r for r in scored if r["arm"] == arm]
        stats = summary["arms"][arm]
        assert stats["planned"] == len(group)
        assert stats["targetHit5"] == statistics.mean(r["targetHit5"] for r in group)
        if not all(r["status"] == "completed" for r in group):
            assert stats["accuracy"] is None and stats["coverage"] is None
        else:
            assert stats["accuracy"] == statistics.mean(r["correct"] for r in group)
    calls = read(output / "calls.json")
    prediction_lookup = {f"{r['seed']}:{r['id']}:{r['arm']}": r for r in predictions}
    retrieval_lookup = {f"{r['seed']}:{r['id']}:{r['arm']}": r for r in rows}
    for call in calls:
        payload = json.loads(call["request"]["input"]["messages"][1]["content"])
        if call["role"] == "answer":
            assert set(payload) == {"currentTime", "question", "options", "evidence"}
            pred = prediction_lookup[call["identity"]]
            assert payload["options"] == queries[pred["id"]]["options"]
            assert [e["sourceId"] for e in payload["evidence"]] == retrieval_lookup[call["identity"]]["selectedIds"]
            for evidence in payload["evidence"]:
                assert evidence["text"] == sources[evidence["sourceId"]]["text"]
        elif call["role"] == "judge":
            assert set(payload) == {"question", "answer", "evidence"}
    result = {"verified": True, "uniqueQuestions": len(queries), "rows": len(rows),
              "sharedHardPools": True, "baselineReproduced": True, "rankingsIdenticalAcrossSeeds": True,
              "futureSourcesExcluded": True, "sourceSpansVerified": True, "predictionGoldMatchingVerified": True,
              "answerCallsObserved": sum(c["role"] == "answer" for c in calls),
              "modelRequestPayloadsChecked": sum(c["role"] in ("answer", "judge") for c in calls),
              "answerEvaluationComplete": summary["complete"], "modelCallsByVerifier": 0}
    (output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    verify(parser.parse_args().output)
