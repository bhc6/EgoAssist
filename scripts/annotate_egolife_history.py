"""Attach validity corrections without rewriting historical experiment numbers."""
import argparse
import hashlib
import json
from pathlib import Path


def annotate(directory):
    directory = Path(directory)
    originals = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in directory.glob("*.json") if p.name != "validity.json"}
    value = {"schema": "experiment-validity.v1", "evaluationRole": "historical-exploratory",
             "independentTest": False, "safeRecallClaimSupported": False,
             "originalFileSha256": originals,
             "reasons": ["Existing samples and former frozen split were exposed to tuning.",
                         "Legacy routes did not share a verified hard-constraint candidate pool.",
                         "Seed labels did not isolate fitting from testing."],
             "supersededBy": "docs/EGOLIFE_EXPERIMENT_PROTOCOL_2026-09-12.md"}
    if directory.name.startswith("memory-comparison"):
        value["reasons"].append("Target frame metadata was included in queries/indexed records/answer context.")
    (directory / "validity.json").write_text(json.dumps(value, indent=2), encoding="utf8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    args = parser.parse_args()
    for directory in args.directories:
        annotate(directory)
