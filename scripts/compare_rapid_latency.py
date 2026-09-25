"""Alternate real rapid-interaction runs with transport optimizations on/off."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    paths = [Path(__file__), ROOT / "scripts/verify_rapid_interaction.py",
             ROOT / "egoassist/realtime/decisions_router.py", ROOT / "egoassist/realtime/modular.py",
             ROOT / "egoassist/application/composition.py", ROOT / "egoassist/application/realtime_connection.py",
             ROOT / "egoassist/realtime/turn_coordinator.py", *sorted((ROOT / "egoassist/prompts").glob("*.json"))]
    plan = {"createdAt": datetime.now(timezone.utc).isoformat(),
            "scope": "Same prompt v8 and application; alternate transport pool/TTS prefetch on-off. Three authored questions at three gaps per run. Not a held-out quality benchmark or p95.",
            "modes": {"baseline": {"EGOASSIST_DECISIONS_CONNECTION_POOL": "0", "EGOASSIST_TTS_PREFETCH": "0"},
                      "optimized": {"EGOASSIST_DECISIONS_CONNECTION_POOL": "1", "EGOASSIST_TTS_PREFETCH": "1"}},
            "repeats": args.repeats,
            "hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    (args.output / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {**plan, "runs": [], "passed": False}
    for repeat in range(args.repeats):
        for mode in (("baseline", "optimized") if repeat % 2 == 0 else ("optimized", "baseline")):
            output = args.output / f"{repeat + 1}-{mode}"
            env = {**os.environ, **plan["modes"][mode], "EGOASSIST_TURN_ROUTER_BACKEND": "openrouter-decisions"}
            with (args.output / f"{repeat + 1}-{mode}.log").open("w", encoding="utf-8") as log:
                process = subprocess.run([sys.executable, str(ROOT / "scripts/verify_rapid_interaction.py"),
                                          "--output", str(output)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            path = output / "report.json"
            result = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            row = {"repeat": repeat, "mode": mode, "exitCode": process.returncode, "passed": result.get("passed", False),
                   "cases": [{"gapMs": c["intervalMs"], "passed": c["passed"], "turns": c["turns"]}
                             for c in result.get("cases", [])], "evidence": str(path)}
            report["runs"].append(row)
            (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"mode": mode, "repeat": repeat, "passed": row["passed"],
                              "firstAudioMs": [c["turns"][-1].get("firstAudioMs") for c in row["cases"]]}), flush=True)
    report["summary"] = {}
    for mode in plan["modes"]:
        cases = [c for r in report["runs"] if r["mode"] == mode for c in r["cases"]]
        rows = [t for c in cases for t in c["turns"]]
        def median(key, values):
            samples = [v[key] for v in values if key in v]
            return statistics.median(samples) if samples else None
        report["summary"][mode] = {"questions": len(rows), "passed": sum(t.get("passed", False) for t in rows),
            "medianReceiptMs": median("receiptMs", rows), "medianRoutingMs": median("routingMs", rows),
            "medianLatestAudioMs": median("firstAudioMs", [c["turns"][-1] for c in cases]),
            "medianColdLatestAudioMs": median("firstAudioMs", [c["turns"][-1] for c in cases if c["gapMs"] == 0]),
            "medianLaterLatestAudioMs": median("firstAudioMs", [c["turns"][-1] for c in cases if c["gapMs"] != 0])}
    report["hashMismatches"] = [p for p, h in plan["hashes"].items() if hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h]
    report["passed"] = all(r["passed"] for r in report["runs"]) and not report["hashMismatches"]
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"]), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=3)
    raise SystemExit(0 if run(parser.parse_args()) else 1)
