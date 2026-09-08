"""Measure local SQLite memory cost; synthetic fixtures, no provider latency."""
import argparse
import json
import importlib.util
import statistics
import sys
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from egoassist.memory.timeline import TimelineMemory


def benchmark(output: Path, fixtures: Path | None = None, compare_ref: str | None = None):
    output.mkdir(parents=True, exist_ok=False)
    report = {"fixture": "synthetic", "providerMeasured": False, "sizes": []}
    baseline_class = None
    if compare_ref:
        # Compare locally trusted repository code, without changing the checkout.
        source = subprocess.run(["git", "show", f"{compare_ref}:egoassist/memory/timeline.py"],
                                check=True, capture_output=True, encoding="utf-8").stdout
        name = "egoassist.memory.timeline_benchmark_baseline"
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader=None))
        sys.modules[name] = module
        exec(compile(source, "baseline_timeline.py", "exec"), module.__dict__)
        baseline_class = module.TimelineMemory
        report["baselineRef"] = compare_ref
        report["comparison"] = "alternating old/new reads on identical SQLite fixtures"
    for size in (100, 1000, 5000):
        path = (fixtures or output) / f"{size}.sqlite3"
        if fixtures and not path.is_file():
            raise ValueError(f"missing synthetic fixture: {path}")
        memory = TimelineMemory(path)
        baseline = baseline_class(path) if baseline_class else None
        writes = []
        for index in range(size if fixtures is None else 0):
            start = time.perf_counter()
            memory.remember(f"物品{index}的存放位置是抽屉{index}。", user_id="bench",
                            case_id="case", scope="case", kind="fact",
                            subject=f"物品{index}", predicate="位置", value=f"抽屉{index}")
            writes.append((time.perf_counter() - start) * 1000)
        timings = []
        before_timings = []
        for trial in range(30):
            models = [(memory, timings), (baseline, before_timings)]
            results = []
            for candidate, measurements in (models if trial % 2 else reversed(models)):
                if candidate is None:
                    continue
                start = time.perf_counter()
                result = candidate.retrieve("物品42的位置是什么？", user_id="bench", case_id="case",
                                            scope="case", query_spec={"subject": "物品42", "predicate": "位置"})
                measurements.append((time.perf_counter() - start) * 1000)
                assert result["selected"][0]["card"]["value"] == "抽屉42"
                results.append([item["cardId"] for item in result["selected"]])
            assert all(result == results[0] for result in results)
        report["sizes"].append({"cards": size, "writeMedianMs": round(statistics.median(writes), 3) if writes else None,
                                "beforeWarmMedianMs": round(statistics.median(before_timings[1:]), 3) if before_timings else None,
                                "readColdMs": round(timings[0], 3),
                                "readWarmMedianMs": round(statistics.median(timings[1:]), 3),
                                "readMaxMs": round(max(timings), 3)})
    (output / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, help="Reuse synthetic databases from an earlier benchmark")
    parser.add_argument("--compare-ref", help="Locally trusted Git revision to compare against")
    args = parser.parse_args()
    benchmark(args.output, args.fixtures, args.compare_ref)
