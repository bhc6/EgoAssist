# EgoAssist Product Re-foundation Charter

Status: Accepted
Effective date: 2026-07-30
Research snapshot: [OSS_RESEARCH_2026-07-30.md](OSS_RESEARCH_2026-07-30.md)

## 1. Why This Re-foundation Exists

EgoAssist has a credible research substrate: full-duplex media, interruption,
auditable turn persistence, scoped memory, tool confirmation, and explicit
failure boundaries. It does not yet have a reliable execution plane for real
life tasks.

The previous architecture made internal state durable before making user
outcomes durable. An intent or job row therefore proves that work was recorded;
it does not prove that work ran, produced an external effect, or reached the
user. This charter resets the project around outcomes that a person can safely
depend on.

## 2. Product Mission

EgoAssist is a privacy-conscious, hands-busy life assistant that helps a person
complete concrete activities while preserving attention and control.

The product must:

- understand one or more explicit user intentions without silently discarding
  older work;
- execute only supported actions through durable, observable workflows;
- continue accepted work across process, network, browser, and device failures;
- make pending work, failures, results, and data use visible to the user;
- use memory to reduce repeated effort without turning inference into fact;
- intervene conservatively when visual or conversational evidence is weak;
- never represent a local acknowledgement or manifest label as a completed
  real-world effect.

## 3. Product Scope

### In scope

- natural full-duplex voice and camera interaction;
- foreground conversations plus explicit durable jobs;
- reminders and timers with a configured delivery endpoint;
- task guidance with step-scoped reference artifacts;
- optional human-help connectors with explicit data preview and consent;
- checkpoint-constrained visual evidence for progress and hazard hypotheses;
- participant and case memory with provenance, retention, export, and deletion;
- a user-facing Task Center and Result Inbox;
- reproducible research traces and outcome-based evaluation.

### Not in scope

- an unconstrained autonomous agent that invents background work;
- safety-critical medical, legal, or emergency decision making;
- claims of expert escalation without a configured and acknowledged endpoint;
- automatic task completion based only on one ungrounded model frame;
- hidden always-on recording or indefinite transcript retention;
- a second generic agent graph layered over the durable workflow engine.

## 4. Capability Truth Policy

A capability may be shown as available only when all six layers exist:

1. a typed user contract;
2. a real executor or connector;
3. durable lifecycle and idempotency semantics;
4. a result and delivery path;
5. a user-visible control surface;
6. black-box end-to-end tests of the promised outcome.

Anything missing a layer must be marked `experimental`, `demo-only`, or
`unsupported`. The values `accepted`, `scheduled`, `completed`, and `delivered`
are reserved for evidence-backed state transitions.

### Current capability truth

| Capability | Current status | Product statement |
|---|---|---|
| Full-duplex audio/video and barge-in | Implemented | Retain and migrate behind the realtime port |
| Durable completed transcripts | Implemented | Retain; add retention and export controls |
| Scoped TimelineMemory and MemoryDraft | Implemented research substrate | Retain as an EgoAssist differentiator |
| Work Ledger | Infrastructure only | It is a projection, not an executor |
| Background intent execution | Unsupported | Hide until a durable workflow runs it |
| Timer/reminder delivery | Unsupported | The deadline calculator is not a timer |
| Human help | Demo-only | A local acknowledgement is not escalation |
| Reference display | Unsupported | No artifact resolver or viewer is connected |
| Structured realtime task perception | Experimental/offline | Adapter is not wired to the live task path |
| Skill capability labels | Descriptive metadata | Never expose them as executable features |
| Online evaluation | Offline/replay only | No online metric without truth collection |

## 5. Foundation Decisions

The selected stack is intentionally small. Each selected component owns one
kind of hard infrastructure; EgoAssist owns the product semantics.

| Concern | Foundation | Ownership decision |
|---|---|---|
| Realtime media and browser transport | LiveKit server, browser SDK, and Agents | Adopt; place existing provider bridge behind a port |
| Durable execution, retry, timers, signals | Temporal server and Python SDK | Adopt as execution source of truth |
| External tool interoperability | Official MCP Python SDK | Adopt for connector boundary, not internal domain calls |
| Domain schemas and validation | Pydantic | Adopt for commands, events, results, and API payloads |
| Product persistence and migrations | SQLAlchemy plus Alembic | Adopt; support SQLite locally and PostgreSQL for deployment |
| Notification delivery | Browser notifications plus ntfy adapter | Adopt behind a `NotificationPort` |
| Reference artifacts | PDF.js and a simple image viewer | Adopt; EgoAssist selects task-scoped artifacts |
| Tracing and metrics | OpenTelemetry | Adopt; keep research events as a separate projection |
| PII detection support | Presidio plus EgoAssist recognizers | Adopt as defense in depth, not as the consent authority |

The following are deliberately not selected now:

- Pipecat: strong and active, but overlaps with the selected LiveKit interaction
  runtime. Reconsider only if transport independence becomes a measured need.
- LangGraph: useful for agent graphs, but Temporal already owns durable control
  flow. A second lifecycle engine would recreate split-brain state.
- Restate: promising, but Temporal currently has clearer licensing and a more
  mature selected deployment path for this project.
- APScheduler: allowed for isolated local experiments only. It is not the
  production execution source of truth.
- Chatwoot: may be supported through an optional connector. It is not embedded
  as a core dependency while its repository license is reported as `Other`.

## 6. Target Architecture

```text
Browser / mobile client
        |
        v
LiveKit room and media transport
        |
        v
EgoAssist Interaction Core
  - turn attribution and speech arbitration
  - intent decomposition and focus
  - memory and consent policy
  - safety and capability policy
  - checkpoint-constrained perception
        |
        +-----------------------+
        |                       |
        v                       v
Temporal workflows          Conversation response
  - durable jobs              and immediate UI state
  - retries and timers
  - cancellation/signals
  - idempotent activities
        |
        v
MCP / native connector ports
  - notifications
  - reference artifacts
  - human help
  - approved external actions
        |
        v
Result and delivery events -> Product DB projections -> Task Center / Result Inbox
```

### Sources of truth

- Temporal owns executable workflow state and timers.
- The product database owns users, devices, consent, memory, artifacts, and UI
  projections.
- TimelineMemory owns EgoAssist behavioral memory semantics.
- SessionJournal remains a bounded research and diagnostic trace. It must not be
  the only source of product lifecycle truth.
- The Work Ledger becomes a read model populated from workflow and interaction
  events. It must not independently advance executable jobs.

## 7. Canonical Product Data Model

The migration must introduce explicit typed records instead of storing provider
payloads as canonical domain state.

### Identity and data policy

- `User`: ownership boundary and account lifecycle.
- `Device`: notification endpoints, capabilities, and last acknowledgement.
- `ConsentGrant`: purpose, data categories, destination, expiry, and revocation.
- `RetentionPolicy`: per-category retention and deletion schedule.

### Interaction

- `ConversationTurn`: durable transcript, source, provider IDs, language, and
  retention class.
- `Intent`: user goal, kind, confidence evidence, focus state, and source turns.
- `IntentRelation`: split, continuation, dependency, replacement, or conflict.
- `TaskPlan`: versioned plan accepted by policy and, where required, by the user.

### Execution

- `Job`: workflow ID, intent ID, executor kind, idempotency key, deadline,
  cancellation policy, and terminal outcome.
- `JobAttempt`: lease, attempt number, start/end, connector request ID, error,
  retry classification, and committed external effect.
- `Artifact`: reference, image, document, or structured output with provenance.
- `Result`: typed outcome linked to intent, job, and optional artifact.
- `DeliveryAttempt`: endpoint, deduplication key, acknowledgement, and failure.

### Memory and evidence

- `MemoryCard` and `MemoryDraft`: retain existing scoped and typed semantics.
- `Observation`: frame age, checkpoint constraints, model, confidence, and source.
- `AuditEvent`: bounded operational evidence with redaction and retention class.

### Required invariants

- Every accepted job reaches a terminal state or a visible `waiting-input` state.
- No job can be completed by model wording or response completion.
- External effects require a connector receipt and stable idempotency key.
- Workflow execution is at-least-once; user-visible effects are deduplicated.
- A result remains pending until a delivery endpoint acknowledges it or the user
  dismisses it.
- Topic focus never cancels an accepted job without explicit policy or user action.
- Capability availability is computed from configured executors and endpoints,
  not manifest strings.

## 8. User Data Handling Baseline

### Data classes

| Class | Default treatment |
|---|---|
| Raw audio/video | Transport only; do not persist by default |
| Transcript turns | User-visible history with configurable retention |
| Behavioral memory | Explicit scope, provenance, export, correction, and deletion |
| Tool and connector payloads | Minimum necessary fields, encrypted at rest |
| Operational traces | Redacted, sampled, separately retained |
| Research exports | Explicit study consent and immutable dataset version |

### Mandatory controls

- TLS for all non-loopback transport and encryption at rest for deployed data.
- A provider disclosure showing what leaves the device and for which purpose.
- Preview and confirmation before sending personal context to a human-help or
  third-party connector.
- Account export and deletion that cover transcripts, memory, jobs, results,
  artifacts, and connector references.
- Configurable retention jobs with evidence of deletion.
- No raw transcript or tool argument in metrics labels.
- Presidio-assisted redaction plus Chinese and domain-specific recognizers;
  policy and user consent remain authoritative.

## 9. Migration Program

### M0 - Truth reset

- Mark timer, human help, reference display, background execution, structured
  perception, and online evaluation according to the current truth table.
- Remove false `accepted` effects from demo adapters.
- Add the Task Center shell, including honest unsupported and failed states.

Exit gate: no UI, prompt, manifest, or API implies an unavailable external effect.

### M1 - Foundation ports and data contracts

- Define Pydantic commands/events and executor, notification, artifact, and
  perception ports.
- Introduce SQLAlchemy models and Alembic migrations without deleting existing
  journals or TimelineMemory data.
- Add OpenTelemetry correlation IDs across turn, intent, workflow, job, and result.

Exit gate: schemas round-trip, migrations are reversible, and old cases remain readable.

### M2 - Reliable timer vertical slice

- Run a Temporal workflow per accepted timer.
- Deliver through an acknowledged browser or ntfy endpoint.
- Surface create, cancel, fire, retry, duplicate, and offline states in Task Center.

Exit gate: a timer survives server and browser restart, fires once in the user
experience, can be cancelled, and never reports success without delivery evidence.

### M3 - Interaction transport and multiple intents

- Introduce LiveKit behind an `InteractionTransport` port.
- Split one turn into zero or more typed intents.
- Keep foreground focus independent from durable workflow lifecycle.
- Preserve barge-in, provider attribution, and the Result Inbox.

Exit gate: concurrent conversation, timer, and waiting-input intents recover after
disconnect without cross-talk or silent loss.

### M4 - Reference artifacts and human help

- Resolve step-scoped reference artifacts and display them with PDF.js/image UI.
- Add a real human-help connector with consent preview, receipt, acknowledgement,
  timeout, and failure states. Keep the capability unavailable when unconfigured.

Exit gate: both features have connector receipts and user-visible outcomes.

### M5 - Structured perception

- Feed fresh frames, current checkpoint, allowed transitions, and task references
  into the perception adapter.
- Treat output as evidence, not completion truth.
- Require temporal consistency or confirmation before checkpoint advancement.

Exit gate: stale and unrelated frames cannot advance a task; hazards and progress
are evaluated against annotated sequences.

### M6 - Privacy and evaluation hardening

- Implement export, deletion, retention, redaction, and provider disclosure.
- Collect outcome truth through user acknowledgement, connector receipts, and
  annotated research tasks.
- Publish only metrics that have a real producer and denominator.

Exit gate: privacy controls and outcome metrics pass black-box tests.

## 10. Product Acceptance Tests

Release gates are user outcomes, not internal state assertions:

- accepted work never remains indefinitely in `waiting-result`;
- a timer survives process restart and disconnected browsers;
- duplicate workflow/activity delivery produces one user-visible reminder;
- an interrupted response cannot complete a job;
- a late tool call cannot execute under another intent or Skill version;
- two completed jobs remain visible and are each eventually delivered;
- cancelling speech does not cancel a job; cancelling a job does;
- a reference request opens the correct current-step artifact;
- human help exposes submitted, acknowledged, resolved, failed, and timed-out states;
- visual evidence includes checkpoint constraints and frame age;
- account export and deletion cover every user-owned data category.

## 11. Engineering Rules

- Prefer one mature owner per infrastructure concern.
- Add no dependency without an ADR-level ownership and failure-boundary decision.
- Keep provider payloads at adapters; domain state uses versioned typed schemas.
- Keep research observability separate from user-facing product state.
- Test crash points between every external effect and local commit.
- Feature flags may hide incomplete work; they may not rename it as complete.
- Preserve current memory semantics unless a migration demonstrates equivalent
  provenance, scope, and deletion behavior.

## 12. Implementation Evidence

This ledger records verified implementation, not planned capability. A milestone
is marked complete only after its exit gate and regression suite pass.

| Milestone | Status | Evidence |
|---|---|---|
| M0 Truth reset | Complete | Capability catalog drives prompts, provider tools, APIs, and the Task Center; unavailable adapters return `unsupported` or `demo-only`; the local pseudo-scheduler is disabled; full regression and frontend syntax checks pass. |
| M1 Foundation | Complete | Pydantic contracts, typed infrastructure ports, SQLAlchemy models, reversible Alembic migration `b0688201c8ee`, OpenTelemetry ID filtering, and an idempotent JSONL compatibility projector are implemented and tested. Runtime state now projects each case journal into the product DB without replacing TimelineMemory. |
| M2 Reliable timer | Complete | Temporal SDK `1.31.0` owns durable timers, retry, signals, and workflow IDs. The activity writes a stable ResultInbox item before optional ntfy delivery, records per-attempt receipts, and deduplicates repeated activity execution. Task Center supports create/cancel/result acknowledgement. Official time-skipping server tests cover worker restart, fire, cancel, duplicate activity, disconnected UI recovery, and conditional capability exposure. |
| M3 Interaction transport | Not started | Existing provider bridge remains active pending LiveKit migration. |
| M4 Artifacts and human help | Not started | Both remain unavailable or demo-only. |
| M5 Structured perception | Not started | Realtime checkpoint-constrained perception remains experimental. |
| M6 Privacy and evaluation | Not started | Existing memory deletion remains preserved; complete governance is pending. |

The current memory remediation is retained as EgoAssist-owned product research,
not foundation debt. `TimelineMemory`, `MemoryDraft`, participant/case scope,
provenance, temporal semantics, conflict handling, retrieval injection, and
user-controlled deletion must round-trip through every subsequent migration.
