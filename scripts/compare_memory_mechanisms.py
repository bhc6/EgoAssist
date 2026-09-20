"""Real Qwen comparison over locally downloaded EgoLife source annotations.

Text-ingestion experiment, not camera/VLM or external-framework validation.
Gold answers are saved for human review, never passed to the answer model.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist._env import load_env
from egoassist.config import Config
from egoassist.memory.store import HierarchicalMemory, MemoryItem
from egoassist.memory.snapshot import load_memory, save_memory
from egoassist.memory.timeline import TimelineMemory
from egoassist.prompts import prompt_text
from egoassist.retrieval.index import FlatIndex


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def dataset(root, samples):
    stems = {p.stem for p in root.glob("*.mp4")}
    rows = json.loads((root / "EgoLife_QA.json").read_text(encoding="utf-8"))
    rows = [r for r in rows if Path(r["video"]).stem in stems and r.get("source")]
    records, questions, seen = [], [], {}
    for row in rows:
        if row["source"] in seen:
            continue
        sid = "source-" + str(len(records))
        seen[row["source"]] = sid
        locator = {"clip": row["id"], "frames": [row["start_frame"], row["end_frame"]]}
        records.append({"sourceId": sid, **locator, "text": row["source"]})
        questions.append({"question": row["conversations"][0]["value"].replace("<speech>", "").replace("<image>", "").strip(),
                          "target": locator, "expected": row["conversations"][1]["value"], "goldSourceId": sid})
    indices = np.linspace(0, len(questions) - 1, min(samples, len(questions)), dtype=int)
    return records, [questions[i] for i in indices]


async def compare(args):
    load_env(str(ROOT / ".env"))
    key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not key:
        raise ValueError("QWEN_API_KEY required; no simulated fallback")
    args.output.mkdir(parents=True, exist_ok=False)
    records, questions = dataset(args.dataset, args.samples)
    write_json(args.output / "dataset.json", {"records": records, "questions": questions})
    import dashscope
    base = dashscope.base_http_api_url.rstrip("/")
    audit = []
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True,
            headers={"Authorization": "Bearer " + key}) as client:
        async def request(endpoint, body):
            started = time.perf_counter()
            entry = {"endpoint": endpoint, "request": body}
            try:
                async with client.post(base + endpoint, json=body) as response:
                    payload = await response.json()
                    entry.update(status=response.status, response=payload)
                    if response.status != 200:
                        raise RuntimeError(f"provider HTTP {response.status}")
                    return payload
            except Exception as exc:
                entry["error"] = type(exc).__name__ + ": " + str(exc)
                raise
            finally:
                entry["latencyMs"] = round((time.perf_counter() - started) * 1000, 3)
                audit.append(entry)
                write_json(args.output / "provider-audit.json", audit)

        async def embed(texts, text_type):
            vectors = []
            for offset in range(0, len(texts), 10):
                payload = await request("/services/embeddings/text-embedding/text-embedding", {
                    "model": args.embedding_model, "input": {"texts": texts[offset:offset + 10]},
                    "parameters": {"text_type": text_type, "dimension": 1024}})
                vectors.extend(row["embedding"] for row in sorted(payload["output"]["embeddings"], key=lambda r: r["text_index"]))
            vectors = np.asarray(vectors, dtype=np.float32)
            return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-8)

        texts = [json.dumps({"sourceId": r["sourceId"], "text": r["text"]}, ensure_ascii=False) for r in records]
        queries = [q["question"] for q in questions]
        started = time.perf_counter()
        vectors = await embed(texts, "document")
        embedding_write_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        query_vectors = await embed(queries, "query")
        embedding_query_ms = (time.perf_counter() - started) * 1000
        np.savez_compressed(args.output / "embeddings.npz", documents=vectors, queries=query_vectors)
        backend = SimpleNamespace(dim=vectors.shape[1])
        memory = HierarchicalMemory(backend, Config(short_term_capacity=8), index=FlatIndex(backend.dim))
        timeline = TimelineMemory(args.output / "timeline.sqlite3")
        write_times = []
        for i, (record, vector) in enumerate(zip(records, vectors)):
            # Provider embeddings replace frame embeddings to isolate storage/retrieval.
            item = MemoryItem(id=memory._new_id(), t_start=i, t_end=i + 1,
                              embedding=vector, caption=texts[i])
            memory.short_term.append(item)
            memory._items_by_id[item.id] = item
            memory._index.add(item.id, vector)
            start = time.perf_counter()
            timeline.remember(texts[i], user_id="benchmark", case_id="egolife", scope="case",
                              kind="event", source_ids=[record["sourceId"]], card_id=record["sourceId"])
            write_times.append((time.perf_counter() - start) * 1000)
        memory.consolidate()
        save_memory(memory, args.output / "snapshot")
        restored = HierarchicalMemory(backend, Config(), index=FlatIndex(backend.dim))
        load_memory(restored, args.output / "snapshot")
        modes = ("recent-raw", "timeline-lexical", "raw-vector", "hierarchical-vector", "hierarchical-sources")
        results = []
        for qi, question in enumerate(questions):
            for mode in modes:
                started = time.perf_counter()
                if mode == "recent-raw":
                    selected = list(reversed(records))
                elif mode == "timeline-lexical":
                    found = timeline.retrieve(queries[qi], user_id="benchmark", case_id="egolife", scope="case", limit=5)
                    selected = [json.loads(r["card"]["text"]) for r in found["selected"]]
                elif mode == "raw-vector":
                    selected = [records[i] for i in np.argsort(vectors @ query_vectors[qi])[::-1][:5]]
                else:
                    hits = restored.retrieve_by_vector(query_vectors[qi], 5, expand_sources=mode == "hierarchical-sources")
                    selected = []
                    for item in hits:
                        # Expand source records to retain inspectable, valid citation IDs.
                        selected.extend(records[i] for i in (item.child_ids or [item.id]))
                evidence, used = [], 0
                for record in selected:
                    size = len(json.dumps(record, ensure_ascii=False))
                    if used + size > args.budget:
                        continue
                    evidence.append(record)
                    used += size
                retrieve_ms = (time.perf_counter() - started) * 1000
                messages = [{"role": "system", "content": prompt_text("memory_comparison/answer")},
                            {"role": "user", "content": json.dumps({"question": question["question"],
                             "evidence": [{"sourceId": r["sourceId"], "text": r["text"]} for r in evidence]}, ensure_ascii=False)}]
                row = {"mode": mode, "questionIndex": qi, **question, "sourceRecall": question["goldSourceId"] in {r["sourceId"] for r in evidence},
                       "evidenceIds": [r["sourceId"] for r in evidence], "evidenceChars": used,
                       "retrievalMs": round(retrieve_ms, 3)}
                started = time.perf_counter()
                try:
                    payload = await request("/services/aigc/text-generation/generation", {
                        "model": args.model, "input": {"messages": messages},
                        "parameters": {"temperature": 0, "enable_thinking": False, "max_tokens": 350,
                                       "result_format": "message", "response_format": {"type": "json_object"}}})
                    row["output"] = json.loads(payload["output"]["choices"][0]["message"]["content"])
                    row["usage"] = payload.get("usage")
                    row["citationIdsValid"] = set(row["output"]["sourceIds"]).issubset(row["evidenceIds"])
                except Exception as exc:
                    row["error"] = type(exc).__name__ + ": " + str(exc)
                row["answerMs"] = round((time.perf_counter() - started) * 1000, 3)
                results.append(row)
                write_json(args.output / "results.json", results)
            print(f"question {qi + 1}/{len(questions)} completed", flush=True)
    summary = {"dataset": str(args.dataset), "qaSha256": hashlib.sha256((args.dataset / "EgoLife_QA.json").read_bytes()).hexdigest(),
               "records": len(records), "questions": len(questions), "model": args.model, "embeddingModel": args.embedding_model,
               "budgetChars": args.budget, "embeddingWriteMs": embedding_write_ms, "embeddingQueryBatchMs": embedding_query_ms,
               "timelineWriteMedianMs": statistics.median(write_times), "snapshotSourceRecords": len(restored._items_by_id),
               "snapshotActiveRecords": len(restored.all_items()), "modes": {},
               "claimBoundary": "Real provider calls over EgoLife QA source annotations for downloaded clips. No video inference, physical hardware, external memory framework runtime, or calibrated answer-accuracy claim. Text embeddings used for both vector legs. Recent-raw is a bounded-context baseline, not ContextHarness compaction. Source recall is exact gold annotation ID presence; citations alone do not validate answers. Day1 is not a calendar date."}
    for mode in modes:
        group = [r for r in results if r["mode"] == mode]
        summary["modes"][mode] = {"sourceRecall": sum(r["sourceRecall"] for r in group) / len(group),
            "retrievalMedianMs": statistics.median(r["retrievalMs"] for r in group),
            "answerMedianMs": statistics.median(r["answerMs"] for r in group),
            "errors": sum("error" in r for r in group), "invalidCitations": sum(r.get("citationIdsValid") is False for r in group)}
    write_json(args.output / "summary.json", summary)
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "data_real/egolife/A1_JAKE_DAY1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--budget", type=int, default=6000)
    parser.add_argument("--model", default="qwen-flash")
    parser.add_argument("--embedding-model", default="text-embedding-v3")
    asyncio.run(compare(parser.parse_args()))
