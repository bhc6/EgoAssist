# ADR-006: User Data Governance and Real Outcome Evidence

Date: 2026-07-30  
Status: Accepted

## Decision

EgoAssist exposes one account-governance boundary over the existing owners:

* `IdentityStore` owns credentials and participant/case ownership.
* `ProductDatabase` owns the SQLAlchemy projection and retention policies.
* `TimelineMemory` owns `TimelineMemory` cards and `MemoryDraft` records,
  including participant/case scope, provenance, temporal semantics, conflict
  status, retrieval evidence, and user-controlled deletion.
* `PrivacyGovernance` only orchestrates export, deletion, retention, provider
  disclosure, and bounded evidence. It does not create a second state machine.

Account export is a versioned JSON object containing identity metadata, product
records, journals, TimelineMemory/MemoryDraft data, and evaluation outcomes.
Account deletion is explicit (`confirm=true`), idempotent, and removes all
user-owned records from those owners plus journals and outcome evidence. The
remaining deletion proof contains only a subject digest, timestamp, evidence
ID, and counts; it never copies transcript, tool arguments, or memory text.

Retention policies are per user and category (`transcript`, `observation`,
`operational`, `memory`). Execution returns counts-only evidence. Operational metrics use
`safe_metric_attributes`, which rejects transcript, prompt, payload, message,
and tool-argument labels. PII redaction is applied to observability projections
and includes email, Chinese mobile numbers, identity-card numbers, labelled
Chinese names/addresses, and credential-shaped fields. Redaction is not a
replacement for consent or deletion.

The provider disclosure is user-visible and states data categories, purpose,
retention, and controls for realtime dialogue, structured vision, and the
configured human-help connector. Non-loopback transport must use TLS; Fernet
envelopes are available for exported JSON while deployed databases require an
encrypted volume.

Evaluation is evidence-first. `OutcomeLedger` accepts only user
acknowledgements, connector receipts, and annotated perception sequences. Each
metric publishes a denominator and source provenance. The existing replay
intervention evaluator remains available as an offline research tool and is
not labelled online truth.

## Mature component audit (checked 2026-07-30)

The implementation keeps infrastructure on mature upstream components:

| Component | Locked | Registry check | Ownership |
|---|---:|---:|---|
| LiveKit browser | 2.21.0 | npm modified 2026-07-23 | media transport |
| PDF.js | 6.2.108 | npm modified 2026-07-28 | artifact rendering |
| Temporal Python SDK | 1.31.0 | lockfile | durable workflow |
| cryptography | 49.0.0 | lockfile | envelope crypto |
| SQLAlchemy/Alembic | lockfile | migration tests | product projection |

Presidio is optional and is not silently claimed as installed. The fallback
recognizers are deliberately narrow and auditable; deployments that need
additional language coverage should configure Presidio rather than treating
the fallback as complete DLP.

## Failure boundaries

Provider payloads stop at adapters. Export and deletion operate by owner and
are safe to retry. A crash before deletion evidence is written leaves source
records removed but no success response; the operation can be retried and the
new evidence counts zero. A crash during a retention batch leaves a bounded
count and can be rerun. No provider or model response can mark an evaluation
outcome as true without a receipt, acknowledgement, or annotated expected
label.

## Verification

`tests/test_privacy_governance.py` covers export completeness, deletion
completeness/idempotency, retention counts, PII redaction, metric-label
exclusion, encrypted export round-trip, and provider disclosure. Existing
product, realtime, perception, and console suites remain green.
