"""Real typed-decision probe on existing development cases. No runtime actions."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from egoassist._env import load_env
from egoassist.eval.openrouter_decisions import DecisionsClient, DEFAULT_MODEL
from egoassist.prompts import prompt_bundle, ROOT
from verify_router_latency import CASES


def expected_for(name):
    # Development labels are never included in provider inputs.
    return {
        "audience": "other" if name == "other-person" else "assistant",
        "speech": "stop" if name == "stop" else "continue" if name in {"negated-stop", "task-pause"} else "none",
        "task": {"task-completed": "confirm", "task-pause": "pause"}.get(name, "none"),
        "memory": {"save": "save", "recall": "recall"}.get(name, "none"),
        "reminder": name == "compound",
    }


def summarize(rows):
    good = [r for r in rows if r["result"]["status"] == "ok"]
    costs = [(r["result"].get("response") or {}).get("usage", {}).get("cost") for r in rows]
    return {
        "attempts": len(rows), "validResponses": len(good),
        "statusCounts": dict(Counter(r["result"]["status"] for r in rows)),
        "allFieldsCorrect": sum(r["passed"] for r in rows),
        "perFieldCorrect": {k: sum(r.get("checks", {}).get(k, False) for r in rows) for k in expected_for("greeting")},
        "validResponseMedianMs": statistics.median(r["result"]["latencyMs"] for r in good) if good else None,
        "providerReportedCost": sum(costs) if costs and all(isinstance(c, (int, float)) for c in costs) else None,
    }


def run(args):
    load_env()
    client = DecisionsClient(os.getenv("OPENROUTER_API_KEY", ""), model=args.model, timeout=args.timeout)
    bundle = prompt_bundle("decisions_research")
    questions = bundle["questions"]
    args.output.mkdir(parents=True, exist_ok=False)
    report = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "scope": "Exposed synthetic Chinese development cases; typed subset, not full router equivalence or product acceptance.",
        "model": args.model, "promptVersion": bundle["version"], "questions": questions,
        "noulThreshold": 0.5, "thresholdScope": "Uncalibrated development scoring only, no execution authority.",
        "repeats": args.repeats, "independentCases": len(CASES),
        "frozenFiles": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [
            Path(__file__), ROOT / "decisions_research.json", Path(__file__).with_name("verify_router_latency.py"),
            Path(__file__).resolve().parents[1] / "egoassist/eval/openrouter_decisions.py"]},
        "cases": [{"name": name, "text": text, "expected": expected_for(name)} for name, text, _ in CASES],
        "rows": [],
    }
    (args.output / "plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        for repeat in range(args.repeats):
            for name, text, _ in CASES:
                state = {"utterance": text, "speaker": {"status": "unknown", "wearer": False},
                         "activeTask": {"step": "准备鸡蛋、番茄、油和盐", "status": "active"} if name.startswith("task-") else None}
                result = client.decide(json.dumps(state, ensure_ascii=False), questions)
                expected = expected_for(name)
                predicted, checks = {}, {}
                if result["status"] == "ok":
                    predicted = {k: v["noul"] >= 0.5 if v["type"] == "noul" else v["choice"] for k, v in result["answers"].items()}
                    checks = {k: predicted[k] == v for k, v in expected.items()}
                row = {"repeat": repeat, "case": name, "expected": expected, "predicted": predicted,
                       "checks": checks, "passed": bool(checks) and all(checks.values()), "result": result}
                report["rows"].append(row)
                report["summary"] = summarize(report["rows"])
                (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"case": name, "repeat": repeat, "status": result["status"],
                                  "passed": row["passed"], "ms": result["latencyMs"]}), flush=True)
                if result.get("httpStatus") in {401, 402, 403, 429}:
                    return 2
    finally:
        client.close()
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    return 0 if all(r["passed"] for r in report["rows"]) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=1)
    raise SystemExit(run(parser.parse_args()))
