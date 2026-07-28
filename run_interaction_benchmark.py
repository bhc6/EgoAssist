#!/usr/bin/env python3
"""Aggregate provider-neutral proactive-assistance predictions from JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from egoassist.realtime.evaluation import InterventionRecord, summarize_interventions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSONL records with expected/predicted actions")
    args = parser.parse_args()
    records = []
    for line in Path(args.input).read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(InterventionRecord(**json.loads(line)))
    print(json.dumps(summarize_interventions(records), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
