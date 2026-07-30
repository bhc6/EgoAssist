# ADR 004: Task Artifacts and Consent-Gated Human Help

Date: 2026-07-30
Status: Accepted and implemented in M4

## Decision

PDF.js owns PDF parsing, canvas rendering, page navigation, and zoom. EgoAssist
owns the authorization decision: a manifest-catalogued artifact is visible only
to its user and case while its Skill, task, and checkpoint are current. Paths
must resolve beneath the Skill root. Every descriptor carries a checksum and
provenance, and every content read creates an access receipt. A small native
image viewer uses the same authorization endpoint; no second gallery framework
is introduced without a measured need.

Human help uses a narrow webhook port rather than a bundled help-desk product.
The connector is unavailable unless explicitly configured. The browser must
first request a durable preview containing the exact purpose, message, current
task context, data categories, destination origin, and expiry. A second request
with explicit consent submits that immutable preview with a stable idempotency
key. The request, consent, connector receipt, Intent, Job, and ResultInbox item
remain in the case journal.

`request_human_help` is deliberately absent from the provider tool surface.
Model text or a generic tool confirmation cannot send personal context to a
third party. `show_reference` may be model-visible because it has no external
side effect and still resolves against the current checkpoint at execution.

## Failure Behavior

- Unknown, escaped, stale-Skill, stale-task, and stale-checkpoint artifacts fail
  closed without revealing a filesystem path.
- A missing webhook makes human help `unconfigured`, never accepted or queued.
- HTTP errors and client errors produce a durable `failed` result; connector
  deadline expiry produces `timed-out`.
- A repeated confirmed submission returns the stored receipt and does not call
  the connector again. A crash after an external effect can retry the same
  `Idempotency-Key`; the external connector is responsible for effect dedupe.
- Submitted, acknowledged, and resolved receipts describe the connector's
  evidence. They do not claim that a human solved the user's underlying task.

## Verification

The M4 black-box suite starts a real local HTTP webhook and verifies exact
preview gating, bearer authentication, stable idempotency, acknowledgement,
HTTP failure, timeout, repeated-submit dedupe, terminal Job state, and visible
ResultInbox outcomes. Artifact tests verify PDF bytes, current-checkpoint
metadata, tool resolution, access receipts, and authorization revocation after
Reset. PDF.js `6.2.108` was rechecked against the npm registry on the
implementation date, and the npm production audit reported zero vulnerabilities.

All migrations preserve EgoAssist's owned memory innovation: TimelineMemory,
MemoryDraft, participant/case scope, provenance, temporal semantics, conflict
handling, retrieval injection, and deletion behavior are not delegated to
PDF.js or the human-help connector.
