# EgoLife vector retrieval regression protocol

## Scope

Test actual Qwen `text-embedding-v3` embeddings on unchanged SRT cue text from
the consumed EgoLife DAY2-DAY7 holdout. This is a post-test regression experiment,
not a new independent test. Only JAKE is represented. No answer generation or
LLM judge is involved.

Use 30 question texts and all 110,384 source occurrences ending by the latest
question cutoff. There are 87,631 distinct document texts. Identical texts share
an embedding but keep separate source IDs and timestamps in the retrieval index.
Do not merge occurrences or add adjacent text in this experiment. No question
options, answer keys or target timestamps are sent to the embedding provider.

## Fixed configuration

- Model: Qwen `text-embedding-v3`, dimension 1024.
- Provider roles: `document` for sources, `query` for questions.
- Normalize valid vectors to unit length and rank by cosine similarity.
- Exact dense search over all visible source occurrences, not an approximate
  index. Source ID breaks score ties deterministically.
- Candidate budget: 256 for each independent candidate generator; answer
  evidence selection: at most five source occurrences.
- Seeds 17, 43 and 91 control the order in which Timeline gates are evaluated.
  Embeddings are computed once and reused. Seeds do not create independent
  model samples or increase the number of distinct questions beyond 30.
- Existing source, identity, temporal, status and kind constraints remain in
  force. A semantic match can replace lexical-overlap ranking, but it cannot
  override a hard rejection.

## Arms

| Arm | Candidate generation | Selection |
| --- | --- | --- |
| timeline | Existing bounded lexical/recent candidate pool | Existing Timeline selection |
| vector-same-pool | Identical Timeline hard-eligible pool | Descending cosine similarity |
| vector-full | Exact vector Top 256 among currently visible sources, then Timeline hard gates | Descending cosine similarity among survivors |
| rrf-union | Union of Timeline and full-vector hard-eligible pools | Pure-rank RRF with constant 60 |

The fusion arm can have up to 512 candidates before overlap, twice the maximum
single-channel budget. It is an exploratory union comparison, not evidence that
fusion wins under equal compute. After hard rejection, the vector arm does not
refill from lower-ranked global candidates. That policy remains fixed for this run.

The baseline lexical ranking used by RRF is the ordering of Timeline's complete
hard pool, including zero lexical scores. RRF and vector selection do not apply
Timeline's lexical minimum score of 0.05; no cross-model score threshold is
claimed to be calibrated. The same-pool arm isolates reranking without changing
candidate eligibility. The full-vector arm additionally changes candidate
generation.

## Boundaries and evaluation

Each seed starts from a backup of the prior test database. Queries are processed
in descending cutoff order; records not yet completed at the cutoff are removed
from the copy. Dense candidate generation independently filters on original SRT
end time before Top 256 selection. The unchanged original database and historical
freeze are retained.

The new `candidate_card_ids` argument only nominates IDs for Timeline checking.
Scope, active status and allowed kinds are SQL predicates before card processing.
Existing time, identity, reference, entity and scene checks then determine
`hardCandidates`. An empty nomination list must remain empty; unknown or denied
IDs cannot cause fallback to unrelated records. The default retrieval path is
unchanged.

Save all selected source IDs before loading `gold-private.json`. Then score
caption overlap with the official target point: `start < target + 1` and
`end >= target`, with `end <= question cutoff`. Report available target captions,
candidate hit, Top 1 hit and Top 5 hit. These are localization proxies, not
standard evidence Recall, MRR or nDCG; relevance labels covering all supporting
evidence are not available. Report question-day/hour group macro rates with the
explicit caveat that these are temporal blocks, not actual video IDs.

Baseline selections must reproduce the previous 30 results exactly. Require
time, identity, correction and source-boundary regressions. Do not promote a
retrieval configuration solely from this consumed test or its point-time proxy.

## Measurement

Cache embeddings by model, dimension, provider role and exact text in SQLite.
Audit every request attempt, provider-reported token usage, failure and request
latency; save valid normalized vectors incrementally. Request batch size is 10,
with eight workers. Retry transient failures at most five attempts.

Report embedding wall time and query/document call counts separately from local
retrieval time. Local retrieval timings exclude API embedding time, backups,
future-record deletion, output serialization and gold scoring. The same-pool
timing includes the shared full-matrix cosine calculation used by this diagnostic
runner; it is not an optimized same-pool implementation. Fusion timings sum the
measured channel work and ranking work; they are not a parallel-serving latency.
OS and SQLite cache hit rates, monetary cost, answer accuracy and evidence support
remain null when not measured. Do not compare these local retrieval timings with
end-to-end spoken-answer latency.

## Files

- Runner: `scripts/test_egolife_vector_retrieval.py`.
- Verifier: `scripts/verify_egolife_vector_retrieval.py`.
- Safety tests: `tests/test_vector_candidate_boundaries.py`.
- Initial run/cache directory: `artifacts/egolife-vector-retrieval-20260914`.
- Validated replay: `artifacts/egolife-vector-retrieval-20260914-r2`. It fixes
  cutoff serialization to use the same milliseconds as stored timestamps;
  all source/query embeddings are reused. See the [results](EGOLIFE_VECTOR_RESULTS_2026-09-14.md).
- `plan.json` freezes data hashes, runner hash and Timeline hash before provider
  execution. This explanatory protocol was written while embedding was running,
  before any question retrieval results were generated or scored.
