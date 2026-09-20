# EgoLife vector retrieval results, 2026-09-14

Real vector retrieval improves the target-time localization proxy, but the
remaining loss is large. No configuration was promoted to the default path.
This is a consumed-holdout regression experiment, not an independent test.

## Validated comparison

Model: Qwen `text-embedding-v3`, 1024 dimensions. Original SRT text is unchanged.
The corpus contains 110,384 source occurrences, represented by 87,631 distinct
document vectors; identical text retains each occurrence's source ID and time.
Thirty query vectors are computed using the provider's `query` role.

Seeds 17, 43 and 91 each evaluate the same 30 questions in four arms, yielding
360 prediction rows. Embeddings are reused; seeds change gate-evaluation order,
not provider sampling. All candidate and selected-ID lists agree across seeds.
The table therefore reports counts out of 30 distinct questions, not 90.

| Arm | Target in hard pool | Target in Top 1 | Target in Top 5 | Local retrieval median ms |
| --- | --- | --- | --- | --- |
| Existing Timeline | 4/30 | 0/30 | 0/30 | 127.520 |
| Vector rerank, identical hard pool | 4/30 | 1/30 | 2/30 | 139.308 |
| Full-corpus vector retrieval, then hard gates | 8/30 | 2/30 | 2/30 | 230.038 |
| Lexical/vector union with pure-rank RRF | 8/30 | 0/30 | 2/30 | 352.850 |

These are caption overlap counts at official target timestamps, not standard
evidence Recall, MRR, nDCG or answer accuracy. Twenty-nine questions have an
available target-overlap caption. Other captions or transcripts can support an
answer without overlapping the point annotation. No answer model or judge ran.

The 25 query-day/hour temporal groups have macro Top 5 localization rates of
0% for Timeline and 4% for each other arm. These groups are not actual video IDs;
the study remains one participant and cannot establish cross-person generalization.

## Interpretation

- Same-pool reranking selects target captions for questions 380 and 230. It
  improves ranking but cannot change the four-question candidate localization.
- Full-vector retrieval selects targets for questions 380 and 373, both at rank
  one. Its candidate-hit questions are 493, 429, 380, 373, 325, 230, 213 and 123.
  This demonstrates additional semantic candidate coverage with unchanged text.
- The two vector arms have equal Top 5 counts but different successes. Full
  retrieval adds question 373 and loses question 230 from the final Top 5.
- RRF selects the same two target-hit questions as full-vector retrieval but
  neither at rank one. Its union can use up to twice the candidate budget; this
  run provides no evidence of a Top 5 gain from that additional work.
- Full-vector first observed loss is 21 questions outside the nominated Top 256,
  six with a hard-eligible target outside Top 5, two selected, and one with no
  available target-overlap caption. No target in its nominated set is lost solely
  to hard gates in this sample. Other nominated sources are still rejected.
- Questions 483, 391 and 276 have empty hard pools in every arm. Vector similarity
  does not repair missing time or identity support.

Vector search is helpful but insufficient in this setup. The next useful
preprocessing comparison holds this model and retrieval configuration fixed and
compares individual cues with traceable adjacent-cue windows and time-aligned
caption/transcript event units. The present run does not measure that change
and cannot prove that fragmentation is the principal remaining cause.

## Cost and latency accounting

| Measurement | Value |
| --- | --- |
| Successful document batches | 8,764 |
| Successful query batches | 3 |
| Request attempts including retries | 8,774 |
| Recovered connection errors | 7 |
| Provider-reported document tokens | 989,435 |
| Provider-reported query tokens | 369 |
| Total provider-reported tokens | 989,804 |
| Initial embedding wall time | 867.525 seconds |
| Request latency median, all attempts | 672.742 ms |
| Request latency median, query batches | 590.329 ms |
| Additional API calls in corrected replay | 0 |
| Corrected replay cache hits, source occurrences / queries | 110,384 / 30 |
| Monetary cost | null |
| Answer accuracy / evidence support | null / null |
| OS / SQLite cache hit rates | null / null |

Embedding requests used batches of ten and eight concurrent workers. Request
counts and tokens describe cache construction, not 360 answer-model calls.
The corrected replay reads the existing cache; its summary's request count is
the cache's cumulative audit, not fresh calls in that replay.

Local retrieval timings exclude embedding API time, database copying and
rewinding, serialization and gold scoring. Same-pool timing includes the shared
full-matrix cosine calculation in this diagnostic implementation. Fusion timing
sums component work. The corrected budget diagnostic ran concurrently during
part of the final replay; these timings are exploratory observations under
uncontrolled cache/load conditions, not isolated serving benchmarks.

## Replay correction and validation

The first vector run completed, but verification found one baseline hard-pool
source mismatch for question 325. Its replay compared a seconds-only ISO cutoff
against stored millisecond ISO timestamps. A source ending exactly at the cutoff
was incorrectly removed: `21:56:57.000+00:00` sorts after `21:56:57+00:00`.
All historical Top 5 selections had still matched, so checking selected IDs
alone did not detect the problem.

The corrected replay uses the same millisecond precision as stored timestamps.
The first run is retained with `RUN_STATUS.md`; its embeddings remain valid.
The corrected r2 replay and the earlier budget diagnosis were rerun without
additional embedding calls. Main localization counts are unchanged.

Validation completed:

- 360 unique prediction rows, complete across four arms and three seeds.
- All original baseline hard pools and selected IDs reproduced exactly.
- Same-pool equality, union boundaries, future-source exclusion and seed
  consistency verified from the saved predictions.
- All 87,661 cached vectors have the expected dimension, finite values, unit
  norm and complete exact-text/model/role cache coverage.
- All 30 actual vector nomination sets were passed through Timeline gates again;
  hard pools, original texts and source IDs match the saved results.
- Frozen runner, Timeline and dataset hashes match the corrected plan.
- 90 distinct focused tests passed across candidate boundaries, replay cutoff,
  diagnostic audit, retrieval policy, scaling, Timeline memory, grounding and
  official dataset handling. This is not a claim of running the entire suite.

## Artifacts

- [Protocol](EGOLIFE_VECTOR_PROTOCOL_2026-09-14.md).
- Validated run: `artifacts/egolife-vector-retrieval-20260914-r2` contains
  `plan.json`, `predictions.json`, `scored.json`, `summary.json`,
  `embedding-summary.json` and `verification.json`.
- Original cache/audit: `artifacts/egolife-vector-retrieval-20260914/embedding-cache.sqlite3`.
- Runner: `scripts/test_egolife_vector_retrieval.py`.
- Verifier: `scripts/verify_egolife_vector_retrieval.py`.
- Corrected budget diagnostic: `artifacts/egolife-retrieval-diagnosis-20260914-r2`.

The optional `candidate_card_ids` entry point is implemented, bounded and tested.
It nominates sources for the existing gates; it does not enable a production
semantic index or change the default lexical retrieval policy.
