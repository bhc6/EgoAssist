"""Select public benchmark IDs from metadata only; never load answers or run models.

Input inventories must record pinned upstream revisions and hashes. LongMemEval
metadata may come from the official oracle file, but execution must use the full
S history. MemoryAgentBench inventory records retain every question's original
row and index. This script does not implement a model evaluation runner.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from fractions import Fraction
import hashlib
import json
from pathlib import Path


def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def allocate(counts, total):
    """Hamilton allocation with exact arithmetic and a documented lexical tie."""
    population = sum(counts.values())
    if not 0 <= total <= population or any(n <= 0 for n in counts.values()):
        raise ValueError("Invalid population or sample size")
    if not population:
        raise ValueError("Empty population")
    exact = {key: Fraction(n * total, population) for key, n in counts.items()}
    quotas = {key: x.numerator // x.denominator for key, x in exact.items()}
    ranked = sorted(counts, key=lambda key: (-(exact[key] - quotas[key]), key))
    for key in ranked[:total - sum(quotas.values())]:
        quotas[key] += 1
    return quotas


def choose(rows, n, seed, namespace):
    if len({row["sample_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate sample IDs")
    def order(row):
        value = f"{seed}|{namespace}|{row['sample_id']}".encode("utf-8")
        return hashlib.sha256(value).hexdigest(), row["sample_id"]
    return sorted(rows, key=order)[:n]


def sample_lme(inv, size, seed):
    groups = defaultdict(list)
    for item in inv["records"]:
        row = dict(item, sample_id=item["question_id"])
        if row["abstention"] != row["question_id"].endswith("_abs"):
            raise ValueError("Abstention flag disagrees with official ID convention")
        key = row["question_type"] + ("|abstention" if row["abstention"] else "|answerable")
        groups[key].append(row)
    counts = {key: len(rows) for key, rows in groups.items()}
    quotas = allocate(counts, size)
    if any(quotas[key] == 0 for key in groups):
        raise ValueError("Sample does not cover every type/answerability stratum")
    selected, strata = [], []
    for key in sorted(groups):
        picks = choose(groups[key], quotas[key], seed, inv["revision"] + "|" + key)
        selected.extend(dict(r, stratum=key, inclusion_probability=quotas[key]/counts[key]) for r in picks)
        strata.append({"stratum": key, "population": counts[key], "sample": quotas[key]})
    selected_ids = {r["sample_id"] for r in selected}
    remaining = sorted(r["question_id"] for r in inv["records"] if r["question_id"] not in selected_ids)
    return {"dataset": inv["dataset"], "revision": inv["revision"],
            "population": sum(counts.values()), "sample_size": len(selected),
            "strata": strata, "selected": selected, "remaining_ids": remaining,
            "execution_history": "Full longmemeval_s_cleaned history, never the oracle history",
            "s_history_id_crosscheck_complete": False}


def sample_mab(inv, size, seed):
    groups = defaultdict(lambda: defaultdict(list))
    for row in inv["records"]:
        count = row["question_count"]
        hashes = row["question_hashes"]
        if len(hashes) != count:
            raise ValueError("Question hashes do not cover all questions")
        ids = row.get("question_ids") or []
        if ids and len(ids) != count:
            raise ValueError("Official question IDs have inconsistent length")
        for qi in range(count):
            item = {"sample_id": f"{row['split']}:{row['row_index']}:{qi}",
                    "split": row["split"], "source": row["source"],
                    "row_index": row["row_index"], "question_index": qi,
                    "official_question_id": ids[qi] if ids else None,
                    "question_sha256": hashes[qi], "context_sha256": row["context_sha256"],
                    "context_characters": row["context_characters"]}
            groups[row["split"]][row["source"]].append(item)
    capability_counts = {k: sum(len(v) for v in sub.values()) for k, sub in groups.items()}
    capability_quotas = allocate(capability_counts, size)
    selected, strata = [], []
    for split in sorted(groups):
        counts = {source: len(items) for source, items in groups[split].items()}
        quotas = allocate(counts, capability_quotas[split])
        if any(quotas[source] == 0 for source in counts):
            raise ValueError("Sample does not cover every public subtask/length configuration")
        for source in sorted(counts):
            picks = choose(groups[split][source], quotas[source], seed, inv["revision"] + "|" + split + "|" + source)
            selected.extend(dict(r, inclusion_probability=quotas[source]/counts[source]) for r in picks)
            strata.append({"capability": split, "source": source,
                           "population": counts[source], "sample": quotas[source]})
    contexts = {(r["split"], r["row_index"]) for r in selected}
    return {"dataset": inv["dataset"], "revision": inv["revision"],
            "population": sum(capability_counts.values()), "sample_size": len(selected),
            "capability_counts": capability_counts, "capability_quotas": capability_quotas,
            "strata": strata, "selected": selected, "selected_contexts": len(contexts),
            "execution_history": "Full original context and official incremental order/chunk policy",
            "capabilities_must_not_be_merged_across_incompatible_metrics": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lme-inventory", type=Path, required=True)
    parser.add_argument("--mab-inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lme-size", type=int, default=100)
    parser.add_argument("--mab-size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261001)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; use a new path to preserve the selection")
    inputs = [json.loads(p.read_text(encoding="utf-8")) for p in (args.lme_inventory, args.mab_inventory)]
    if any(not i.get("revision") for i in inputs):
        parser.error("Every inventory requires a pinned upstream revision")
    lme = sample_lme(inputs[0], args.lme_size, args.seed)
    mab = sample_mab(inputs[1], args.mab_size, args.seed)
    lme_selected = {r["question_id"] for r in lme["selected"]}
    lme_all = {r["question_id"] for r in inputs[0]["records"]}
    overlapping = [r for r in mab["selected"] if r["source"] == "longmemeval_s*" and r["official_question_id"] in lme_all]
    manifest = {"schema_version": "public-memory-pilot.v1", "created_date": "2026-10-01",
                "status": "selection_draft_not_frozen_not_evaluated", "seed": args.seed,
                "selection_uses_question_or_answer_semantics": False,
                "selection_rule": "Hamilton quotas with lexical ties; SHA256 rank by seed/revision/stratum/sample ID",
                "intended_role": "Blinded first stage after development; not an iterative tuning set",
                "sampling_script_sha256": sha_file(__file__),
                "input_inventory_sha256": {p.name: sha_file(p) for p in (args.lme_inventory, args.mab_inventory)},
                "datasets": [lme, mab], "selected_questions": len(lme["selected"])+len(mab["selected"]),
                "overlap_audit": {"mab_selected_question_ids_present_in_full_lme": len(overlapping),
                    "mab_selected_question_ids_present_in_selected_lme": sum(r["official_question_id"] in lme_selected for r in overlapping),
                    "overlapping_ids": [r["official_question_id"] for r in overlapping],
                    "independent_replication_claim_allowed": False,
                    "history_family_and_near_duplicate_audit_complete": False},
                "freeze_requirements": ["S-file ID and data-hash check", "project exposure and near-duplicate audit", "history-family grouping", "models/prompts/scorer/chunking pins", "cost caps and analysis freeze"],
                "results": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "questions": manifest["selected_questions"],
                      "lme_types": dict(Counter(x["question_type"] for x in lme["selected"])),
                      "lme_abstention": sum(x["abstention"] for x in lme["selected"]),
                      "mab_capabilities": mab["capability_quotas"], "mab_contexts": mab["selected_contexts"],
                      "overlap": manifest["overlap_audit"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
