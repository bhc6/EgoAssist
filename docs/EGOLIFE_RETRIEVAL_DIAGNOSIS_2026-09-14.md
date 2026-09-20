# EgoLife retrieval loss diagnosis, 2026-09-14

The dominant observed loss is candidate generation, with additional ranking loss.
Increasing the candidate budget alone does not repair the final selection.
This is an exposed regression diagnostic, not another independent test or a
measurement of answer-model quality. No experimental ranker was promoted.

Update after vector-experiment verification: the original replay compared ISO
seconds with stored milliseconds and could drop a source ending exactly at the
query cutoff. The corrected diagnostic was rerun in
`artifacts/egolife-retrieval-diagnosis-20260914-r2`; all localization counts and
loss-stage counts below are unchanged. The table now uses corrected-run timing.
The earlier outputs remain available as historical artifacts.

## Method and validity

- Reused the 30 questions from the completed DAY2-DAY7 JAKE test. These questions
  have now been inspected and cannot be reused as an independent holdout.
- Copied the previous SQLite database; original freeze and results are untouched.
  Processed questions in descending cutoff order, deleting future records from
  the copy. Asserted that every scanned source ended by its question cutoff.
- Ran 256, 1024 and 4096 candidate budgets for each question: 90 retrieval runs.
  Each used the same Timeline time, identity, status and source constraints.
- Compared existing selection against character-bigram BM25 over the identical
  hard-eligible pool. BM25 used document frequencies within that pool, positive
  scores and Top 5; this is not a full-corpus index or a complete B experiment.
- Gold target timestamps were used only after retrieval and reranking to check
  overlap with caption intervals. Neither ranker received gold or answer options.
- All 30 baseline selections exactly reproduced the previous recorded selections.
  Production defaults and existing selection behavior remain unchanged.

## Results

Counts below are questions out of 30, not numbers of retrieved fragments.

| Candidate budget | Target available | Target scanned | Target hard-eligible | Existing Top 5 target hit | Bigram BM25 Top 5 target hit | Timeline median ms |
| --- | --- | --- | --- | --- | --- | --- |
| 256 | 29 | 4 | 4 | 0 | 1 | 122.842 |
| 1024 | 29 | 6 | 6 | 0 | 1 | 192.050 |
| 4096 | 29 | 12 | 11 | 0 | 1 | 426.296 |

These are target-time localization proxies, not standard Recall@5, MRR or nDCG.
The official point timestamp is not a complete relevance annotation: evidence
elsewhere, including transcripts or adjacent captions, may support an answer.
One question has no qualifying caption overlap at any budget. Its evidence is
not proven absent from all source material.

| First observed loss stage | 256 | 1024 | 4096 |
| --- | --- | --- | --- |
| No available target-overlap caption | 1 | 1 | 1 |
| Target outside scanned pool | 25 | 23 | 17 |
| Scanned target rejected by hard gates | 0 | 0 | 1 |
| Hard-eligible target outside selection | 4 | 6 | 11 |
| Target selected | 0 | 0 | 0 |

This attribution stops at the first observed failure. An unscanned target may
also fail a gate or ranking if it is later retrieved; these are not independent
causal effects of each stage.

## Confirmed mechanisms and limits

1. Candidate generation is bounded lexical lookup plus time/recent-record fill.
   A downstream semantic reranker cannot recover sources outside this pool.
   Multiplying the budget by 16 improves hard-pool localization from 4 to 11
   questions but leaves existing Top 5 localization at zero.
2. The score transformation clips FTS BM25 values to 1.0 and sorting breaks ties
   by recency. At every budget, 19 questions have more than five hard-eligible
   candidates scoring 1.0. At budget 256, question 380's target scores 1.0 but
   ranks 53 among audited candidates, with 75 hard candidates scoring 1.0.
   Bigram reranking selects its target at all three budgets. This is evidence
   of lost ranking resolution, not proof that clipping explains every miss.
3. The FTS index uses a trigram tokenizer although query tokenization includes
   bigrams. A minimal in-memory SQLite check with a Chinese phrase for a white
   microwave returns zero matches for its two-character color term, and one
   match for its three-character appliance term. This confirms a short-token
   blind spot; it does not quantify its contribution to the 30 real failures.
4. At budget 4096, question 137's scanned target is rejected for unresolved
   reference. Questions 483, 391 and 276 have no hard-eligible candidates at all
   three budgets. This does not justify disabling identity or temporal checks.
   More budget cannot make unsupported identity or time assertions valid.

## Repair order and promotion conditions

1. **Candidate generation:** compare a corpus-level Chinese bigram BM25 index,
   an independent dense retrieval channel, and their union. Enforce the same
   hard gates before evidence is eligible for answering. Apply equivalent
   owner/status/source SQL filters early and measure rejected-candidate budget
   waste; retain the complete safety checks after retrieval.
2. **Ranking:** compare un-clipped lexical rank and pure-rank RRF on identical
   pools, then ablate lexical and dense channels. Keep these experiments separate
   from candidate-budget changes so improvements can be attributed.
3. **Evidence units:** compare original cues with traceable adjacent-cue event
   windows. Preserve source IDs, exact text, time bounds and query cutoff.
   Resolve references only when source evidence establishes the identity;
   uncertain references remain unavailable or require clarification.
4. **Answering:** after selecting a retrieval configuration on development data,
   calibrate selective answering (E). A refusal policy cannot recover missing
   evidence, so it is not a substitute for the preceding retrieval work.

Use this exposed sample for regression investigation. Freeze a new untouched
question/source split for final comparison, group by actual videos or declared
temporal blocks, and use at least three fixed seeds for stochastic components.
Require time, identity, correction and source-boundary regressions. Measure
Recall@1/@5, MRR and nDCG only against documented evidence relevance labels;
report answer accuracy, coverage and evidence support separately. Only JAKE is
represented, so no cross-participant generalization claim is supported.

## Measurement scope

| Item | Value |
| --- | --- |
| Retrieval runs | 90 |
| Model calls | 0 |
| Model response cache hits | 0 (no model calls) |
| SQLite / OS cache hit rate | null |
| BM25 reranking latency | null |
| End-to-end answer latency | null |
| Monetary cost | null |
| New answer accuracy / support evaluation | null |

Timeline latency includes full candidate-audit construction but excludes BM25
construction/reranking, database backup/deletion and gold scoring. Budgets ran
in fixed ascending order per query in one process; cache effects were not
controlled. The medians describe this diagnostic run, not a randomized latency
benchmark. Retrieval was deterministic; no three-seed model rerun was performed.
The corrected run overlapped with vector replay on the same machine, so these
timings are not an isolated performance comparison.

## Implementation and artifacts

- `TimelineMemory.retrieve(include_candidate_audit=True)` exposes the complete
  bounded scanned pool for diagnostics. Default audit remains capped at 20.
- `tests/test_candidate_diagnostics.py` verifies unchanged selection and hard
  eligibility, including rejection of an ambiguous reference.
- Focused validation: candidate diagnostics, retrieval policy and scaling
  boundary tests, **7 passed**.
- Runner: `scripts/diagnose_egolife_retrieval.py`.
- Corrected outputs: `artifacts/egolife-retrieval-diagnosis-20260914-r2/plan.json`,
  `results.json`, `summary.json`, and a separate `diagnostic.sqlite3`.
- Previous test: [results](EGOLIFE_INDEPENDENT_RESULTS_2026-09-14.md).

The baseline-reproduction assertion was added after the initial run and passed
during the corrected replay. Both saved summaries report
`baselineSelectionsReproduced: true`.
