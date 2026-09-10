"""Profile a retained session on an isolated copy; never modify source evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys
import time
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from egoassist.product.database import ProductDatabase
from egoassist.realtime.context_harness import SessionJournal
from egoassist.realtime.work_ledger import CaseWorkLedger


def profile(source, output, repeats):
    output.mkdir(parents=True, exist_ok=False)
    copied = output / "session.jsonl"
    shutil.copyfile(source, copied)
    journal = SessionJournal(copied)
    database = ProductDatabase(output / "projection.sqlite3")
    rows = journal.entries()
    report = {
        "source": str(source.resolve()), "sourceSha256": hashlib.sha256(copied.read_bytes()).hexdigest(),
        "sourceBytes": copied.stat().st_size, "events": len(rows),
        "measurement": "local replay of retained journal; no model calls or hardware measurement",
        "eventCounts": dict(Counter(row.get("eventType", row["type"]) for row in rows)),
        "measurements": {},
    }
    try:
        for label, operation in (
            ("projection", lambda: database.project_journal(journal, user_id="profile")),
            ("workLedger", lambda: CaseWorkLedger(journal).snapshot()),
        ):
            samples = []
            for _ in range(repeats):
                started = time.perf_counter()
                operation()
                samples.append(round((time.perf_counter() - started) * 1000, 3))
            report["measurements"][label] = {"samplesMs": samples, "medianMs": statistics.median(samples)}
    finally:
        database.close()
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["measurements"], indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("--repeats must be at least 2 to measure warm projection")
    profile(args.source, args.output, args.repeats)
