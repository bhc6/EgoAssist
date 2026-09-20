"""Read-only exposure and frozen-input audit; never selects or reranks evidence."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold() if c.isalnum())


def main(args):
    raw = json.loads(args.prior_qa.read_text(encoding="utf-8"))
    videos = {p.stem for p in args.prior_qa.parent.glob("*.mp4")}
    exposed = [r for r in raw if Path(r.get("video", "")).stem in videos and r.get("source")]
    texts = {norm(r["source"]) for r in exposed}
    source_rows = json.loads((args.dataset / "sources.json").read_text(encoding="utf-8"))
    questions = json.loads((args.dataset / "questions.json").read_text(encoding="utf-8"))
    historical_questions = {norm(c[0].get("value", "").replace("<speech>", "").replace("<image>", ""))
                            for r in raw if (c := r.get("conversations"))}
    matches = [r["sourceId"] for r in source_rows if norm(r["text"]) in texts]
    overlapping_questions = [q["id"] for q in questions if norm(q["question"]) in historical_questions]
    report = {"priorQaSha256": hashlib.sha256(args.prior_qa.read_bytes()).hexdigest(),
              "priorRawQaCount": len(raw), "previouslyLoadedQuestions": len(exposed),
              "previouslyLoadedSources": len(texts),
              "previouslyLoadedVideoDays": dict(Counter(re.search(r"DAY\d+", r["video"]).group() for r in exposed)),
              "officialSelectedQuestions": len(questions), "newSourceRows": len(source_rows),
              "questionExactOverlapIds": overlapping_questions, "sourceExactOverlapIds": matches,
              "questionDuplicateComparison": "all prior local QA, not only the 110 previously evaluated rows",
              "semanticNearDuplicateAudit": None, "providerPretrainingContamination": "unknown",
              "independenceBoundary": "Independent of recorded project development/evaluation, not proof of absence from provider training. Same participant; different days and QA schema."}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"sourceExactOverlapIds", "questionExactOverlapIds"}}))
    print(json.dumps({"sourceExactOverlaps": len(matches), "questionExactOverlaps": len(overlapping_questions)}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--prior-qa", type=Path, default=Path("data_real/egolife/A1_JAKE_DAY1/EgoLife_QA.json"))
    p.add_argument("--output", type=Path, required=True)
    main(p.parse_args())
