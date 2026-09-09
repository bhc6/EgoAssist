"""Reproduce snapshot failures using real dataset text/embedding artifacts."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist.config import Config
from egoassist.memory import snapshot, store
from egoassist.retrieval.index import FlatIndex


def revision_module(ref, path, name):
    source = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT, check=True,
                            capture_output=True, encoding="utf-8").stdout
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader=None))
    sys.modules[name] = module
    exec(compile(source, path, "exec"), module.__dict__)
    return module


def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    records = json.loads((args.source / "dataset.json").read_text(encoding="utf-8"))["records"]
    with np.load(args.source / "embeddings.npz") as archive:
        vectors = archive["documents"]
    old_store = revision_module(args.ref, "egoassist/memory/store.py", "egoassist.memory.recovery_old_store")
    old_snapshot = revision_module(args.ref, "egoassist/memory/snapshot.py", "egoassist.memory.recovery_old_snapshot")
    report = {"baseline": args.ref, "input": str(args.source), "results": []}
    for label, memory_module, persistence in (("baseline", old_store, old_snapshot), ("fixed", store, snapshot)):
        backend = SimpleNamespace(dim=vectors.shape[1])
        memory = memory_module.HierarchicalMemory(backend, Config(short_term_capacity=8), index=FlatIndex(backend.dim))
        for i, (record, vector) in enumerate(zip(records, vectors)):
            item = memory_module.MemoryItem(id=memory._new_id(), t_start=i, t_end=i + 1,
                                            embedding=vector, caption=record["text"])
            memory.short_term.append(item)
            memory._items_by_id[item.id] = item
            memory._index.add(item.id, vector)
        memory.consolidate()
        row = {"version": label, "sourceCount": len(records), "activeCount": len(memory.all_items()),
               "registryCount": len(memory._items_by_id), "lostCaptions": sum(not any(r["text"] in m.caption for m in memory._items_by_id.values()) for r in records)}
        try:
            persistence.save_memory(memory, args.output / label)
            restored = memory_module.HierarchicalMemory(backend, Config(), index=FlatIndex(backend.dim))
            row["restoredCount"] = persistence.load_memory(restored, args.output / label)
            row["loaded"] = True
        except Exception as exc:
            row.update(loaded=False, error=type(exc).__name__ + ": " + str(exc))
        report["results"].append(row)
    (args.output / "recovery.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ref", default="a6a2526", help="Trusted local Git revision to execute")
    run(parser.parse_args())
