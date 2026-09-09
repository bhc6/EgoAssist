# Memory mechanism comparison

The reproducible run is in `outputs/memory-comparison-20260909-2` (ignored runtime artifact). It uses 22 source annotations and 12 questions from the downloaded `A1_JAKE_DAY1` EgoLife subset. Qwen `text-embedding-v3` and `qwen-flash` were called through the real provider; no video inference was performed.

| Path | Exact source recall | Retrieval median |
|---|---:|---:|
| Recent bounded raw context | 58.3% | 0.32 ms |
| Timeline SQLite + FTS lexical | 100% | 15.3 ms |
| Flat vector evidence | 100% | 0.22 ms after embedding |
| Hierarchical centroid only | 75% | 0.51 ms |
| Hierarchical centroid expanded to original sources | 100% | 0.48 ms |

The hierarchical centroid alone is lossy: similar episodes can replace the target event and still produce valid citations. Expansion to retained source items restores recall without adding an ontology. The realtime product therefore keeps SQLite lexical retrieval as the default; cloud embeddings remain an offline comparison because their network latency and failure modes are unsuitable for the hot path.

The old consolidation implementation was also run against the same artifact: it lost 14/22 captions and produced an unloadable snapshot (`Unknown memory id`). The fixed implementation retains all source records, real wall-clock anchors and rebuilds its vector index on load.

These are source-recall and citation-integrity measurements, not calibrated answer accuracy. A model can cite a valid but irrelevant source, so confidence is not inferred from the model's own score. Report generation is asynchronous and auditable, but a 22-record real run still took 14-22 seconds; cancellation and restart recovery are required for predictable operation.
