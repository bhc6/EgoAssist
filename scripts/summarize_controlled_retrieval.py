"""Recompute comparisons from saved rows; never retrieve or call a provider."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

from egoassist.eval.controlled_retrieval import grouped_metrics, paired_bootstrap, write_json


def summarize(root):
    root = Path(root)
    data = json.loads((root / "results.json").read_text(encoding="utf8"))
    runs = {(r["seed"], r["arm"]): r for r in data["runs"]}
    seeds = data["plan"]["seeds"]
    arms = (*data["plan"]["arms"], "E")
    comparisons = {}
    pairs = [("A", arm) for arm in arms if arm != "A"] + [
        ("B_word", "B"), ("B_char", "B"), ("C_semantic", "C"), ("D_sentence", "D")]
    for left, right in pairs:
        comparisons[f"{right}-minus-{left}"] = {
            str(seed): paired_bootstrap(
                [r for r in runs[seed, left]["rows"] if r["split"] == "reused_validation"],
                [r for r in runs[seed, right]["rows"] if r["split"] == "reused_validation"], seed=seed)
            for seed in seeds}
    summaries = {}
    for arm in arms:
        group = [runs[seed, arm] for seed in seeds]
        overall = [grouped_metrics(run["rows"]) for run in group]
        # Report a mean over repeated split assignments, not a larger sample n.
        means = {}
        for family in ("micro", "videoMacro"):
            names = ("recall@1", "recall@5", "mrr", "ndcg@5", "candidateRecall", "evidenceSupport", "answerCoverage", "unsupportedAnswerRate")
            means[family] = {name: statistics.mean(g[family][name] for g in overall)
                            if all(g[family][name] is not None for g in overall) else None for name in names}
        means["retrievalMedianMsPerSeed"] = {str(seed): grouped_metrics(runs[seed, arm]["rows"])["micro"]["retrievalMedianMs"] for seed in seeds}
        means["reusedValidationPerSeed"] = {str(seed): runs[seed, arm]["splits"]["reused_validation"] for seed in seeds}
        means["uniqueQuestions"] = len({r["key"] for run in group for r in run["rows"]})
        means["uniqueVideos"] = len({r["videoId"] for run in group for r in run["rows"]})
        means["answerErrorsPerSeed"] = {str(seed): sum(r.get("answerStatus") != "measured" for r in runs[seed, arm]["rows"]) for seed in seeds}
        summaries[arm] = means
    audit_path = root / "provider-audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf8")) if audit_path.exists() else []
    usage = Counter()
    calls = Counter()
    for row in audit:
        model = row["request"]["model"]
        calls[model] += 1
        for key, value in (row.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                usage[f"{model}:{key}"] += value
    output = {"schema": "controlled-retrieval-summary.v1", "arms": summaries, "pairedVideoComparisons": comparisons,
              "choices": data["choices"], "runtime": data["runtime"], "provider": data["provider"],
              "providerCallsByModel": dict(calls), "reportedTokenUsage": dict(usage), "cost": None,
              "independentTestQuestions": 0, "claimBoundary": data["claimBoundary"]}
    # Runtime assertions use exactly the archived results, including abstentions.
    for seed in seeds:
        baseline = runs[seed, "A"]["rows"]
        expected = {r["key"]: r["candidateIds"] for r in baseline}
        for arm in arms:
            for row in runs[seed, arm]["rows"]:
                assert row["candidateIds"] == expected[row["key"]]
                assert set(row["selectedIds"]) <= set(row["candidateIds"])
    output["sharedHardGateAudit"] = "passed: all seeds/arms have identical eligible IDs; every selected source is eligible"
    write_json(root / "summary.json", output)
    print(json.dumps({"metrics": {arm: value["micro"] for arm, value in summaries.items()},
                      "provider": output["provider"], "calls": dict(calls),
                      "choices": [(c["seed"], c["developmentWinner"], c["selectionPolicy"]["status"], c["selectionPolicy"]["threshold"]) for c in data["choices"]]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    summarize(parser.parse_args().output)
