# EgoAssist

EgoAssist is a minimal full-duplex framework for general embodied assistance. It starts without a domain workflow, observes user intent, and activates a pluggable Skill only when auditable evidence matches. The core does not assume cooking, assembly, a fixed step order, or even that the user has a task.

This repository is an engineering baseline, not a research novelty claim. It keeps the control surface small and exposes uncertainty, routing evidence, memory provenance, response races, and known limitations.

## Quick Start

Python 3.10+ is required. Reuse the repository `.venv` when it exists.

```powershell
cd D:\DEMO\EgoAssist
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe scripts\verify_egoassist.py
.\.venv\Scripts\egoassist.exe --open
```

Open <http://127.0.0.1:8770>, start the full-duplex session, and allow camera and microphone access. Configure `QWEN_API_KEY` or `DASHSCOPE_API_KEY` in `.env`.

The local login is a research identity and audit mechanism, not a production authentication claim. It separates participants and named experiment cases. Each case owns an append-only journal whose header records the participant ID, case label, and initial memory policy; later policy changes are appended as events. Preference and episodic memory can independently use case or participant scope, so cross-case recall is never implicit. Production deployment would require a separate security review and identity system.

The default startup mode is general and unloaded:

```text
activeSkill = null
task        = null
knowledge   = none
```

As a bundled example, saying that you want to cook activates `kitchen-coach` but does not silently choose a recipe. Naming fruit salad activates its task. Unknown or ambiguous input remains in general mode.

## Runtime Flow

```text
browser microphone + camera
       |                 |
       v                 v
dialogue session     perception session
server VAD           no VAD / no user audio
explicit response    explicit text-only gate
       |                 |
       v                 v
transcript -> route -> episodic retrieval
       |                 |
       +-- context ------+-- scene / intervention evidence
               |
        response scheduler
               |
       interruptible speech
```

The provider lanes are separate sessions. The dialogue lane alone owns microphone audio, turn detection, and user-visible output. Server VAD detects and commits a user turn but does not automatically create its response: EgoAssist first processes the transcript, retrieves evidence, updates context, and then explicitly creates one grounded response. The perception lane receives only a sampled frame plus a deterministic silent ordering token required by Qwen's audio-before-image protocol. It cannot consume a user turn or emit browser audio.

The complete research control panel visualizes observable artifacts, not private chain-of-thought:

- media arrival, coalescing, and browser audio settings;
- input/output channel attribution and possible residual echo;
- intent candidates, evidence, scores, rejection reasons, and activation;
- structured observations and checkpoint hypotheses;
- loaded knowledge source and claim boundary;
- memory profile, independent plugin switches/scopes, and preference/episodic retrieval candidates;
- intervention policy, response queue, cancellation, and provider IDs;
- prompt hash, causal parent IDs, latency, and raw event JSON.

## Context Harness

Each browser WebSocket owns an isolated, versioned JSONL session journal under the configured memory directory. The journal is the source of truth; a Qwen realtime session is a disposable projection. Reconnect with `?session=<id>` to restore the latest task checkpoint, typed messages, tool calls/results, pending confirmations, retrieval evidence, compaction checkpoint, interruptions, and provider token usage. A session ID has a single live writer.

Context construction is ordered and auditable: required policy layers are reserved first, durable session history second, and optional layers use only the remaining budget. User transcripts, retrieved episodes, preferences, summaries, and tool output remain low-trust data messages and are never interpolated into system instructions. The manifest reports included/excluded layers, estimated tokens, observed provider usage, remaining budget, compaction ID, and an effective context hash.

When estimated context or observed provider input usage reaches the configured budget, the harness writes a self-contained `summary + retained tail` compaction entry. Tool call/result pairs are retained or removed together. A new compaction ID replaces the online provider session and replays the bounded projection, so compaction affects the real session rather than only local bookkeeping. Completed and interrupted assistant text are committed by response ID before provider replacement.

The default summarizer is deterministic and marks itself as `deterministic-fallback`; deployments can inject a higher-fidelity summarizer callback, whose failure is explicitly recorded before falling back. Relevant environment settings are:

```text
EGOASSIST_CONTEXT_WINDOW=32768
EGOASSIST_CONTEXT_RESERVE=4096
EGOASSIST_CONTEXT_KEEP_RECENT=6000
EGOASSIST_CONTEXT_SUMMARY=3000
```

## Full-Duplex Attribution

The microphone remains active while the assistant speaks. Browser capture requests echo cancellation, noise suppression, and automatic gain control. Protocol events keep the two logical channels separate:

- user: `conversation.item.input_audio_transcription.completed`;
- assistant: `response.audio_transcript.*` only after its exact `response_id` is classified as a user-visible audio request.

Recent assistant output is compared with new input transcription to flag likely residual echo. The flag is observation-only: input is not dropped because a user may intentionally repeat the assistant. This is not biometric speaker diarization, and the UI states that boundary explicitly.

Provider event names are not visibility permissions. Each dialogue response ID is bound to its requested `purpose` and `outputMode`. Only `conversation/audio` and `proactive-utterance/audio` cross the browser playback and caption boundary. Gate artifacts stay in the separate perception session; stale IDs and unattributed dialogue output fail closed and emit `response.output.suppressed`. The browser independently checks the same typed `purpose`.

A Qwen `response idle timeout` or provider close triggers bounded microphone buffering and automatic dialogue-session replacement. The browser WebSocket stays open and shows `reconnecting` followed by a new `ready` event. Perception-session recovery is independent, so one lane cannot corrupt the other's scheduler state.

### Realtime liveness contract

Provider callbacks are treated as at-least-once, possibly missing, duplicated, or reordered events. A dispatched request
must either receive `response.created` within the create deadline and a terminal event within the finish deadline, or
the disposable dialogue projection is replaced. Generation plus request sequence/response ID makes stale watchdogs
no-ops. Reconnect preserves only the highest-priority queued request; it does not replay an orphan that may already have
spoken. A provider-created successor reconciles older active IDs, and terminal-before-created plus duplicate callbacks
are explicitly audited without blocking the queue.

```text
EGOASSIST_RESPONSE_CREATE_TIMEOUT=5
EGOASSIST_RESPONSE_FINISH_TIMEOUT=45
```

The bounds are engineering controls, not conversational policies. `immediate`, `finish-clause`, and `protected` still
decide whether user speech interrupts an attributable response, including while it is waiting for `response.created`.
No natural-language stop phrase is hard-coded. Tool callback delivery uses an atomic canonical `call_id` claim: exact
duplicates are suppressed, conflicting reuse fails closed, and the case journal restores idempotency across reconnects.

Microphone chunks and latest-only camera frames are transport telemetry rather than individual semantic observations.
They are journaled as bounded `media.audio.batch` and `media.video.batch` totals and flushed when the WebSocket closes;
transcripts, decisions, memory events, tool effects, response transitions, and checkpoints remain individually durable.
This avoids an `fsync` for every media chunk without removing counts or byte-volume auditability.

The original freezes recurred because earlier tests assumed one ordered `created -> done` pair, while the real provider
omitted terminal events, delayed `created`, and emitted duplicate tool callbacks. Per-chunk synchronous audit writes
also added avoidable event-loop pressure. The release gate now includes production-timeline replay for missing,
duplicate, and reordered transitions, concurrent tool-call delivery, long scheduler sequences, and high-volume media
aggregation. A passing happy-path suite alone is not considered evidence of realtime liveness.

Turn-taking is Skill-configurable:

- `immediate`: stop ordinary guidance on user speech;
- `finish-clause`: finish the current short clause, then stop;
- `protected`: allow a bounded emergency utterance while input capture continues.

Natural barge-in is triggered by provider speech-start/VAD events, with completed transcription only as a fallback;
it does not inspect a command phrase list. This makes interruption independent of whether the user happens to say
"stop": ordinary speech can interrupt an `immediate` response, while a Skill may choose `finish-clause` or the
bounded `protected` policy. After transcription, one temperature-zero semantic turn classification covers all
language-dependent state changes. It receives only the current user turn plus registered Skill names and descriptions,
returns a strict five-field object, and records its policy version, model, latency, raw output, and decision in the
experiment journal. Negated, quoted, reported, hypothetical, and ambiguous mentions must not be treated as controls.
Classification failure leaves ordinary conversation available but fails closed for state mutation: it cannot silence
the assistant, change Skill/task state, persist a preference, or broaden temporal retrieval.

## Semantic Turn Boundary

`semantic-turn-router-v2` produces exactly these labels once per completed realtime user turn:

| Field | Allowed non-null labels | Effect |
| --- | --- | --- |
| `speechControl` | `stop-speaking` | Cancel/suppress the current spoken response without creating a reply |
| `taskControl` | `confirm-step`, `rollback`, `repeat`, `pause-task`, `resume-task`, `request-help` | Feed a label-only task state machine |
| `preferenceIntent` | `propose-durable` | Create a confirmation candidate; never persist immediately |
| `memoryQuery` | `referential` | Permit timestamp-neighbor expansion for a contextual reference |
| `skill` | registered Skill name, `none`, `ambiguous` | Select or abstain from a progressively disclosed Skill |

The core contains no hidden natural-language phrase tables for these decisions. Protocol enums are allowed because
they are typed control contracts. Skill `activation_phrases`, task titles, and aliases are also allowed, but only as
public, versioned manifest evidence shown in routing traces; they cannot override the semantic Skill decision in the
realtime path. This boundary is guarded by regression tests. The default lightweight classifier and deadline are:

```text
EGOASSIST_TURN_ROUTER_MODEL=qwen-flash
EGOASSIST_TURN_ROUTER_TIMEOUT=3.0
```

`POST /api/voice` is intentionally deterministic for controlled/offline experiments and does not make a cloud model
call. A caller may provide validated `speechControl`, `taskControl`, `preferenceIntent`, and `memoryQuery` research
labels; Skill/task evidence comes from the versioned manifests. Omitting labels means no corresponding state change.

## Silence And Proactive Interaction

Periodic vision checks use the isolated perception session and request text-only structured output:

```json
{
  "speak": false,
  "utterance": "",
  "reason": "none",
  "urgency": "low",
  "scene_summary": "objective visible activity",
  "salient_change": false
}
```

Malformed or schema-invalid gate responses fail closed. A schema-valid but semantically poor gate remains an observable
model error instead of being hidden by a local utterance keyword filter, and should be counted during evaluation. A
provider may violate the requested modality and return gate JSON through an audio-transcript event; the perception lane
parses that artifact internally and has no browser audio route. Only a validated `speak=true` decision can enqueue a
separate `proactive-utterance` on the dialogue lane. A `speak=false` response cannot create user-visible transcript or
audio events. The first scene summary creates an episodic anchor; later summaries are persisted only when
`salient_change=true`.

## Memory Research Baseline

Memory is the main experimental surface of this repository. The baseline is deliberately small and inspectable: it does
not claim to be free of inductive bias, but every online memory rule is deterministic, versioned, visible in runtime
state, and removable as a treatment. Engineering isolation, crash recovery, and audit logging are kept separate from
the behavioral memory hypotheses being studied.

### Systems and storage

EgoAssist currently distinguishes five mechanisms:

| Mechanism | Online behavioral treatment | Current implementation |
| --- | --- | --- |
| Context journal | No; required infrastructure | Typed append-only case history, bounded projection, deterministic extractive compaction |
| Confirmed preference | Yes | Explicit candidate, user confirmation, JSON persistence, exact task/checkpoint retrieval |
| Lexical episodic | Yes | Compact text episodes, lexical Jaccard retrieval, optional timestamp-neighbor expansion |
| Hierarchical visual | No; offline method | Frame segments, embeddings, consolidation, temporal localization, and abstention for EgoLife |
| Knowledge | No; separate baseline component | Static Skill/task context after activation; no runtime semantic knowledge retrieval |

`username` identifies one participant or research identity. A server-generated opaque user ID owns all files; the
username itself is never used as a path. A named case identifies one experimental run. The physical layout is:

```text
identity.sqlite3
users/{user-id}/preferences.json
users/{user-id}/episodes.json
users/{user-id}/cases/{case-id}/preferences.json
users/{user-id}/cases/{case-id}/episodes.json
users/{user-id}/sessions/{case-id}.jsonl
```

The participant stores are the only cross-case stores. Case stores are separate files, including separate episodic
capacity limits, so records and eviction in case A cannot affect case B. Pending preference candidates, recent
decision status, and transient preference events are keyed by case even when confirmed records use participant scope.
Every state, policy, confirmation, deletion, and WebSocket request checks that the case belongs to the authenticated
user. A case ID owned by another user returns `403`; a valid-looking ID cannot select another user's files.

Switching a component from `case` to `participant`, or back, selects another physical store. It does not copy, merge,
or delete records. This is intentional and must be considered when interpreting a mid-case policy change.

### Per-turn order

For each completed realtime user transcript, the online path is:

```text
append transcript to case journal
  -> read the case's versioned memory policy
  -> lexical episodic retrieval (or explicit disabled result)
  -> record retrieval and injection outcome
  -> route Skill/task and update the task checkpoint
  -> persist the current transcript as an episode when enabled
  -> create a preference candidate only for a classified durable preference
  -> exact preference retrieval for the active task/checkpoint
  -> inject selected evidence as typed low-trust data
  -> explicitly request the provider response
```

Retrieval happens before the current transcript is remembered, so a turn cannot retrieve itself. Retrieved summaries,
confirmed preferences, transcripts, tool output, and compaction summaries are data, not system instructions. The
provider projection can contain them, but they cannot change the system, safety, Skill, or user-control policy.

The case journal records positive and negative memory outcomes. Relevant event types include
`episodic.memory.retrieved`, `episodic.memory.injected`, `episodic.memory.written`,
`episodic.memory.deduplicated`, `memory.candidate.proposed`, `memory.retrieval.completed`, `memory.confirmed`,
`memory.rejected`, and `memory.deleted`. Candidate scores, rejection reasons, selected IDs, storage scope, and the
policy changes that produced them remain attributable to one case. The journal contains user transcripts and selected
memory evidence; it is research data, not an anonymized telemetry stream. Deleting a preference removes the active
record but does not rewrite its historical append-only audit event.

### Confirmed preference

A language-dependent decision can only produce the typed label `preferenceIntent=propose-durable`. It creates a
case-local pending candidate; it never writes a confirmed preference. Persistence requires an explicit user action in
the control panel. Accepting while preference memory is disabled is rejected. Rejecting discards the candidate.

Each confirmed record contains its source quote, evidence, and an exact trigger `{task, step}`. Retrieval uses no
embedding, fuzzy match, LLM, or inferred user profile: both task and checkpoint must match exactly and status must be
`confirmed`. Saving another record for the same trigger replaces the earlier active record. Within a running task,
`announced_memory_ids` prevents the same record from producing repeated reminders after its first reuse. That
one-shot suppression is session state, not a learned preference.

### Lexical episodic

The realtime episodic adapter stores compact text summaries only. Raw camera frames and raw audio are not retained.
Exact consecutive duplicates with the same kind, summary, user scope, and session are coalesced. Each physical store
holds at most `300` records.

Retrieval is fully specified:

- lowercase/case-fold the query and record;
- Latin terms match `[a-z0-9]{2,}`; single Latin characters are ignored;
- Chinese characters are concatenated and represented as overlapping character bigrams;
- score is Jaccard overlap, `|query ∩ record| / |query ∪ record|`;
- ineligible user/session scope always scores zero;
- candidates are ordered by score, with newest-first recency as the tie-breaker;
- select at most `5` candidates with score at least `0.05`; expose at most `20` ranked candidates for audit;
- for a classified referential query only, one adjacent utterance before and after each positive lexical anchor may be
  added with `max(0.05, min(0.95, anchor_score * 0.85))`;
- no embedding, vector database, LLM retrieval, learned reranker, domain schema, or raw-media similarity is used.

Selected evidence includes episode ID, timestamp, source, score, retrieval reason, and summary. The UI calls this
`context injection`. It does not call it generation impact: observing an injection does not establish that the output
changed. That claim requires a controlled counterfactual comparison.

### Profiles and controls

The control panel exposes named profiles plus independent enable/scope controls. `all-online` means all behavioral
memory currently connected to the realtime path; it does not imply that the offline hierarchical visual method is
active.

| Profile | Context journal | Confirmed preference | Lexical episodic |
| --- | --- | --- | --- |
| `all-online` (default) | required | on, participant | on, case |
| `context-only` | required | off | off |
| `confirmed-preference` | required | on, participant | off |
| `lexical-episodic` | required | off | on, case |
| `custom` | required | explicit on/off and scope | explicit on/off and scope |

Disabling a behavioral component gates its persistence, retrieval, and injection without deleting stored records.
The policy is normalized and versioned in SQLite, returned in runtime state, and appended as
`memory_policy_changed` whenever it changes. The context journal remains on in every profile because it is the source
of truth for recovery and audit. It is therefore a controlled constant, not a claimed no-memory condition.

"Pluggable" currently means the two online behavioral components can be independently enabled, scoped, composed, and
ablated through a versioned policy. It does not yet mean arbitrary third-party memory classes can be dynamically
loaded from the UI. The offline hierarchical visual pipeline is likewise not presented as a realtime toggle. A future
memory method should add a typed adapter and a named profile rather than silently replace the baseline algorithm.

### Inductive biases and protocol

The baseline is suitable for memory research because its assumptions are few and observable, not because it has none:

| Mechanism | Explicit inductive bias |
| --- | --- |
| Context | Ordered history, bounded recent tail, deterministic extractive summary, fixed token budgets |
| Preference | Explicit confirmation, exact task/checkpoint identity, replacement by trigger, one reuse per task session |
| Episodic | Character/word tokenization, lexical overlap, `0.05` threshold, top-5, recency tie-break, local adjacency |
| Scope | Case isolation by default for episodes; participant transfer by default for confirmed preferences |
| Generation | Evidence is injected before response creation as low-trust context |

These choices are modest enough for an engineering baseline and precise enough to ablate. They should not be hidden
when reporting results. In particular, lexical retrieval favors surface overlap, adjacency favors local discourse,
and participant scope is a transfer condition rather than a neutral storage option.

For a clean experiment:

1. Use one username per participant or research identity; never reuse a username across participants.
2. Create one named case per experimental run.
3. Select and record the profile before opening the realtime WebSocket or collecting the first turn.
4. Do not use a mid-case profile change as a formal control condition. Evidence already projected into the provider
   context or retained in the append-only journal cannot be retroactively withdrawn.
5. Treat `participant` scope as an explicit experimental treatment. Use `case` scope when runs must be independent.
6. Record model, Skill/task versions, profile version, case label, failures, abstentions, and the journal artifact.
7. Evaluate retrieval and generation separately. A selected memory, an injected memory, and a causally changed answer
   are three different outcomes.

The realtime summary adapter and offline hierarchical visual backend intentionally share only a conceptual contract.
This avoids forcing a second visual model into the latency-sensitive dialogue loop and keeps future memory innovations
measurable against the unchanged lexical/confirmed baseline.

## Skills

Bundled Skills live under `egoassist/skills/`. `--skills PATH` replaces the registry with an external collection. A Skill may provide activation evidence, instructions, optional task catalogs, safety rules, tools, proactive cadence, and turn-taking policy. Tasks are not required: a Skill can alter policy, context, and tools without imposing a procedural workflow.

```text
egoassist/skills/my-skill/
|-- SKILL.md
|-- skill.json
|-- tasks/*.json      # optional
`-- references/
```

The bundled kitchen and assembly Skills are examples. They are not privileged core code. Unknown intent abstains instead of selecting the closest domain.

Capability discovery uses progressive disclosure: the provider initially receives only each registered Skill's `name`
and `description`. Full Skill instructions and task knowledge are loaded only after routing selects one Skill. The same
single constrained semantic call used for turn controls may return exactly one registered name, `none`, or `ambiguous`
on every realtime turn, including a domain switch. It receives no task instructions. Versioned manifest evidence is
then used only to identify an exact task inside the semantically selected Skill. The journal records the semantic and
manifest boundaries separately. Greetings, capability questions, and ordinary conversation therefore remain outside
every Skill; a concrete request such as cooking a named dish can activate `kitchen-coach` without a core phrase rule.
The dialogue model has no `activate_skill` tool and cannot override an abstention; domain tools are disclosed only after
the structured router has selected their owning Skill.

Tool names, confirmation policy, argument schema/defaults, adapter type, and local result wording are declared in `skill.json`. The core only validates the active Skill contract and dispatches a registered handler; the browser derives arguments from the same schema. Undeclared tools, parameters, and handlers fail closed.

## Verification

```powershell
# Deterministic tests and release checks
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\verify_egoassist.py

# Bundled example on one WebSocket: General -> Skill only -> exact task
.\.venv\Scripts\python.exe scripts\check_example_skill_routing.py

# Real lightweight turn-classifier regression matrix
.\.venv\Scripts\python.exe scripts\check_semantic_router.py

# Real provider, media ordering, controls, and cancellation races
.\.venv\Scripts\python.exe scripts\check_realtime.py `
  --exercise-seconds 18 --stress-controls --stress-media-gap

# Offline EgoLife hierarchical-memory benchmark
.\.venv\Scripts\egoassist-egolife.exe --help
```

Expected baseline: run the full suite and treat its reported count as authoritative.

## Package Layout

```text
egoassist/
|-- realtime/       # bridge, routing, task control, memories, scheduler, trace
|-- console/        # packaged research console
|-- skills/         # bundled replaceable Skills
|-- memory/         # hierarchical visual-memory research backend
|-- capture/        # stream and event segmentation
|-- retrieval/      # routing and temporal localization for offline memory
|-- backends/       # model adapters
`-- eval/           # evaluation metrics
```

## Deployment Boundary

```bash
docker build -t egoassist .
docker run --rm -p 8770:8770 --env-file .env egoassist
```

The participant login is an experiment-isolation boundary for cases, journals, and memories, not production authentication. The service has no TLS and should remain bound to `127.0.0.1` unless it is placed behind an authenticated gateway. Benchmark reports should distinguish baseline behavior from new methods and record model versions, Skill versions, latency, failures, abstentions, memory retention settings, and routing decisions.

## License

EgoAssist is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for project attribution.
