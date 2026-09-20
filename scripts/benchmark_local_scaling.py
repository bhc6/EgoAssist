"""Measure local long-history costs without cloud calls or existing user data."""

import argparse
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from egoassist.core.journal import SessionJournal
from egoassist.memory.timeline import TimelineMemory
from egoassist.product.database import ProductDatabase
from egoassist.runtime import ThreeLaneRuntime


def measure(action, repeats=20):
    values = []
    for _ in range(repeats):
        started = time.perf_counter()
        action()
        values.append((time.perf_counter() - started) * 1000)
    values.sort()
    return {
        "medianMs": round(values[len(values) // 2], 3),
        "p95Ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 3),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=20000)
    parser.add_argument("--cards", type=int, default=1000)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="egoassist-scaling-") as directory:
        root = Path(directory)
        journal = SessionJournal(root / "case.jsonl", session_id="case")
        for index in range(args.events):
            journal.append(
                "runtime_event", {"sequence": index, "payload": {"metric": index}}
            )
        journal.append(
            "turn_inbox", {"turnId": "question", "text": "lookup", "status": "received"}
        )
        runtime = ThreeLaneRuntime(journal, "user")
        database = ProductDatabase(root / "product.sqlite3")
        database.project_journal(journal, user_id="user")
        runtime.snapshot(observation={}, scheduler={})
        result = {
            "events": args.events,
            "cards": args.cards,
            "state": measure(lambda: runtime.snapshot(observation={}, scheduler={})),
            "databaseProjection": measure(
                lambda: database.project_journal(journal, user_id="user")
            ),
            "journalTail": measure(lambda: journal.entries(tail=160)),
        }
        memory = TimelineMemory(root / "timeline.sqlite3")
        memory.remember(
            "unique parcel pickup code 7392",
            user_id="user",
            case_id="case",
            scope="case",
            kind="fact",
        )
        for index in range(args.cards - 1):
            memory.remember(
                f"ordinary recorded event {index}",
                user_id="user",
                case_id="case",
                scope="case",
                kind="event",
            )
        query = lambda: memory.retrieve(
            "unique parcel pickup code", user_id="user", case_id="case", scope="case"
        )
        result["retrieval"] = {**measure(query, 10), **query()["timing"]}
        result["snapshotCount"] = memory.snapshot(
            user_id="user", case_id="case", scope="case"
        )["count"]
        database.close()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
