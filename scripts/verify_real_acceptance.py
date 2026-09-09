"""Run real-provider software acceptance suites and preserve every outcome.

Requires configured Qwen credentials, Edge/Playwright and a real dataset export.
No mocks or automatic retries. Physical glasses and microphone ASR are not tested.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def verify(output, source):
    source = source.resolve(strict=True)
    records = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(records, dict) or not records.get("records"):
        raise ValueError("A nonempty real dataset export is required")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = [
        ("attribution-" + scenario, "verify_real_turn_attribution.py", ["--scenario", scenario])
        for scenario in ("rapid", "barge-in", "correction", "duplicate-reconnect", "disconnect-inflight", "automatic-completion", "cancellation")
    ] + [
        ("answer-recovery", "verify_real_answer_recovery.py", []),
        ("latency", "benchmark_realtime_latency.py", ["--repeats", "3"]),
        ("memory", "verify_real_memory.py", []),
        ("social-memory", "verify_real_social_memory.py", []),
        ("relative-time", "verify_real_social_memory.py", ["--relative-anchor"]),
        ("skills", "verify_real_skills.py", []),
        ("skill-catalog", "verify_real_skill_catalog.py", []),
        ("skill-lifecycle", "verify_real_skill_lifecycle.py", []),
        ("browser-reports", "verify_browser.py", ["--real-turn"]),
        ("report-recovery", "verify_insight_recovery.py", ["--source", str(source)]),
    ]
    report = {"passed": False, "mock": False, "hardwareMeasured": False,
              "startedAt": datetime.now(timezone.utc).isoformat(), "source": str(source), "cases": []}

    def persist():
        (output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    persist()
    for name, script, arguments in cases:
        started = time.perf_counter()
        row = {"name": name, "status": "running", "passed": False}
        report["cases"].append(row)
        persist()
        print(json.dumps({"case": name, "status": "running"}), flush=True)
        command = [sys.executable, str(ROOT / "scripts" / script), "--output", str(output / name), *arguments]
        with (output / (name + ".log")).open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=900)
                row.update(returncode=result.returncode, passed=result.returncode == 0)
            except (OSError, subprocess.TimeoutExpired) as exc:
                row["error"] = type(exc).__name__ + ": " + str(exc)
        row.update(status="completed" if row["passed"] else "failed",
                   elapsedSeconds=round(time.perf_counter() - started, 3))
        persist()
        print(json.dumps(row), flush=True)
    report["passed"] = all(row["passed"] for row in report["cases"])
    report["finishedAt"] = datetime.now(timezone.utc).isoformat()
    persist()
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.output, args.source) else 1)
