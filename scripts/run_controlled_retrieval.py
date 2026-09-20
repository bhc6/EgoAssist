"""Run preregistered A-E retrieval experiments and ablations."""
import argparse
import hashlib
from importlib.metadata import version
from pathlib import Path
import platform

from egoassist.eval.controlled_retrieval import Provider, evaluate_suite, write_json
from egoassist.eval.egolife_experiments import load_egolife_tree


def snapshot_code(output):
    root = Path(__file__).resolve().parents[1]
    paths = ["egoassist/eval/controlled_retrieval.py", "egoassist/eval/egolife_experiments.py",
             "egoassist/memory/timeline.py", "egoassist/memory/temporal.py", "egoassist/memory/evidence.py",
             "egoassist/memory/grounding.py", "egoassist/core/settings.py", "scripts/run_controlled_retrieval.py"]
    hashes = {}
    for name in paths:
        contents = (root / name).read_bytes()
        hashes[name] = hashlib.sha256(contents).hexdigest()
        target = output / "code-snapshot" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    write_json(output / "code-provenance.json", {"codeSha256": hashes, "python": platform.python_version(),
        "packageVersions": {name: version(name) for name in ("numpy", "requests", "dashscope")},
        "baseline": "Timeline default ranking with common hard gates; no benchmark intent bonuses or source expansion"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data_real/egolife"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seeds", default="0,1,2")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    seeds = tuple(int(x) for x in args.seeds.split(","))
    if len(set(seeds)) < 3:
        parser.error("at least three distinct seeds required")
    records, samples, manifest = load_egolife_tree(args.dataset)
    if not records or not samples:
        parser.error("no locally evaluable sources/questions")
    args.output.mkdir(parents=True, exist_ok=False)
    snapshot_code(args.output)
    manifest.update(evaluationRole="previously-exposed-exploratory", independentTestQuestions=0,
                    participantGeneralization=False)
    write_json(args.output / "manifest.json", manifest)
    report = evaluate_suite(records, samples, args.output, seeds=seeds,
                            provider=Provider(args.output, enabled=not args.offline), workers=args.workers)
    print({"output": str(args.output), "choices": [(r["seed"], r["developmentWinner"]) for r in report["choices"]],
           "provider": report["provider"]}, flush=True)


if __name__ == "__main__":
    main()
