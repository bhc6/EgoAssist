# EgoAssist

EgoAssist is a full-duplex, camera-aware assistance framework for auditable
embodied-AI experiments. It starts in a general conversational mode, activates a
versioned Skill only when the current turn provides sufficient evidence, and keeps
conversation recovery, behavioral memory, tools, and proactive vision behind
separate typed boundaries.

The repository is an engineering baseline. It does not claim production identity,
perfect semantic memory, or guaranteed model correctness. Its goal is to make the
important state transitions observable, deterministic where possible, and safe to
ablate in experiments.

## 2026 Product Re-foundation

The project is being re-founded around dependable user outcomes rather than
internal state alone. The accepted charter, target architecture, data boundaries,
migration gates, and outcome tests are in
[docs/PROJECT_CHARTER_2026.md](docs/PROJECT_CHARTER_2026.md). The supporting open
source review was verified on 2026-07-30 and is recorded in
[docs/OSS_RESEARCH_2026-07-30.md](docs/OSS_RESEARCH_2026-07-30.md).

Current capability truth:

- full-duplex media, interruption, durable transcripts, scoped memory, and tool
  confirmation are implemented research infrastructure;
- Work Ledger is a user-facing projection; Temporal, not the ledger, executes
  configured reminder workflows;
- task-scoped PDF/image references are implemented; remote human help is available
  only when a real webhook is configured and the user approves the exact preview;
- checkpoint-bound structured task perception is implemented as evidence only;
  outcome evaluation accepts real user acknowledgements, connector receipts,
  and annotated sequences, while the replay intervention evaluator remains
  explicitly offline.

The selected migration foundations are LiveKit for realtime transport, Temporal
for durable execution, MCP for external connector boundaries, SQLAlchemy/Alembic
and Pydantic for product data contracts, OpenTelemetry for correlation, and
explicit notification and artifact adapters. EgoAssist continues to own the
differentiating memory, interaction, intent, and safety policies.

## Highlights

- Full-duplex browser audio with camera context and natural barge-in.
- General mode plus progressively disclosed, replaceable Skills.
- One ordered turn worker and one runtime owner per experiment case.
- Reconnectable, append-only case journals with bounded context compaction.
- One persistent SQLite TimelineMemory for facts, events, state, preferences,
  observations, scenes, captures, and successful tool effects.
- Server-authoritative UTC instants plus participant civil time, including DST-aware
  date ranges and separate recording/effective timestamps.
- Persistent typed memory drafts and entity-qualified retrieval; restart or an
  unrelated follow-up cannot silently consume the wrong value.
- Explicit participant and case scopes with no implicit cross-case recall.
- Separate dialogue and perception provider sessions.
- Observable routing, memory provenance, response scheduling, tool execution, and
  failure states in the research console.
- Account-level export, deletion, category retention, provider disclosure, and
  research-evidence denominators are available from the Privacy panel/API.

## Requirements

- Python 3.10 or newer.
- A modern browser with microphone and camera permission.
- `QWEN_API_KEY` or `DASHSCOPE_API_KEY` for the realtime Qwen provider.
- Windows PowerShell for the commands below. The package itself is Python and also
  runs in Linux containers.

## Quick Start

Use the repository virtual environment when it is present:

```powershell
cd D:\DEMO\EgoAssist
Copy-Item .env.example .env
# Set QWEN_API_KEY or DASHSCOPE_API_KEY in .env.
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e ".[dev]"
.\.venv\Scripts\python.exe scripts\verify_egoassist.py
.\.venv\Scripts\egoassist.exe --open
```

Open <http://127.0.0.1:8770>, register a local research identity, create or select
an experiment case, and start the realtime session.

`--open` schedules the browser launch before the new process binds its port. If
`8770` already has a listener, the browser can open that older server and the new
process will then fail to bind. Check and stop the known listener before testing
new code:

```powershell
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8770 -State Listen
Stop-Process -Id <known-egoassist-pid>
.\.venv\Scripts\egoassist.exe --open
```

Do not stop an unidentified process. To run two EgoAssist servers, both the port
and memory root must differ:

```powershell
.\.venv\Scripts\egoassist.exe `
  --port 8771 `
  --memory-path .egoassist-8771/runtime.json
```

The `runtime.json` filename is a compatibility anchor. Persistent state is stored
beside it. A process-lifetime lease prevents two servers from using the same
memory root.

## First Run Behavior

Under the default configuration, a new case starts unloaded:

```text
activeSkill = null
task        = null
knowledge   = none
```

Ordinary conversation stays in General mode. A concrete matching request can
activate a Skill, and an exact task is selected only when task evidence is also
present. Ambiguous or unsupported intent abstains instead of choosing the nearest
domain.

`--active-skill NAME` intentionally changes this rule: newly created cases and
their realtime seed start with that Skill. It is an experiment option, not the
default.

Selecting General mode or Reset is durable. The server invalidates the case epoch,
cancels and awaits the old turn worker, clears the Skill/task/tool projection, and
writes a new checkpoint before reconnect. An older classifier or transcript task
cannot restore the unloaded Skill afterward.

## Runtime Model

```text
browser login -> participant -> experiment case -> authenticated WebSocket
                                                    |
                     +------------------------------+-------------------+
                     |                                                  |
                microphone                                        camera
                     |                                         latest frame
                     +---------------- dialogue lane ------------------+
                                      |
                              completed transcript
                                      |
                         per-case FIFO turn worker
                                      |
                   semantic route + current-turn memory retrieval
                                      |
                      one grounded response request
                                      |
                         interruptible browser audio

                 periodic latest frame -> immutable task/checkpoint context
                                      |
                      isolated perception provider lane
                                      |
                     validated ObservationEvidence
                                      |
             TaskSession confirmation gate + TimelineMemory policy
                                      |
                  optional task-bound utterance on dialogue lane
```

The dialogue lane owns user audio, ordered camera context, and user-visible
responses. The perception lane receives no microphone stream and has no direct
browser audio route. Each frame is bound to the user, case, Skill, task version,
checkpoint epoch, direct allowed path, completion criteria, artifact checksums,
and capture time. Provider output is evidence, not task truth. Malformed, stale,
unrelated, changed-context, and forbidden-path output fails closed. Repeated
fresh evidence may ask for confirmation, but only an explicit user confirmation
advances the checkpoint.

Dialogue video is coalesced until the provider confirms an active speech segment;
the newest frame is then flushed after audio ordering is established. Each VAD
segment emits started, stopped, and exactly one transcribed-or-dropped audit path.
If the provider reports an image-before-audio protocol error, EgoAssist marks open
segments dropped, replaces the disposable dialogue connection, and replays only
the durable context projection. A dropped segment is observable, not invented or
silently represented as remembered text.

The local preview normally runs at the browser's camera rate. The console sends
JPEG frames at selectable 1, 2, or 4 FPS, defaults to 2 FPS, and coalesces pending
frames. Proactive checks are periodic snapshots, not frame-by-frame recognition.
Raw camera frames and microphone chunks are transport data and are not stored as
behavioral memory.

## Concurrency And Recovery

Each live case has one server-side owner containing:

- an atomic WebSocket reservation;
- a mutation lock;
- a monotonically increasing epoch;
- one FIFO user-turn queue and worker;
- one tracked memory-consolidation task.

Every asynchronous turn step verifies that its owner and epoch are still current
before mutating state or creating a response. Case switch, General mode, Reset,
disconnect, and shutdown invalidate or await the relevant work. This prevents
late semantic classification from reversing a newer control action and prevents
adjacent user turns from being submitted out of order.

Transcript state and memory commits remain FIFO. Response creation follows
latest-input-wins: when a newer completed transcript arrives before an older turn
has requested an answer, the older turn is still journaled and committed but its
answer is skipped. Provider speech-start events cancel or defer current playback
according to the active turn-taking policy, so a barge-in does not wait for final
transcription before stopping stale speech.

The response scheduler also uses request sequence and generation IDs. Missing,
duplicated, stale, or reordered provider callbacks cannot silently take ownership
of a newer response. Watchdogs replace a disposable provider projection when a
request never starts or never terminates.

Each case owns an append-only JSONL journal. A reconnect restores the latest
checkpoint, typed conversation items, pending confirmations, task state, routing,
tool idempotency, compaction state, and provider usage. The journal is the recovery
and audit source; it is not searched as long-term behavioral memory.

## TimelineMemory

All online behavioral memory uses `timeline.sqlite3` in WAL mode. The database is
shared physically but every operation requires an owning `user_id`, a scope, and,
for case-scoped data, a `case_id`.

Schema v2 migrates an existing v1 database in place. Legacy `created_at` and
`happened_at` values seed the new recorded/effective columns; old cards remain
readable and are not reclassified by a model during migration.

```text
<memory-root>/
|-- identity.sqlite3
|-- timeline.sqlite3
|-- .egoassist-server.lock
`-- users/<user-id>/sessions/<case-id>.jsonl
```

### Real-world time

The server clock is the authority for `nowUtc` and every `recordedAt`. The browser
sends only its IANA timezone name and current UTC offset as civil-time metadata;
it cannot supply or move the server's current instant. The selected case persists
that timezone, and each turn recomputes local time from a fresh server UTC reading.
With a valid IANA zone, its rules take precedence over the reported offset, so a
long-running case follows daylight-saving transitions.

Timeline cards separate:

- `recordedAt`: when EgoAssist durably accepted the claim, in UTC;
- `effectiveAt` / `effectiveEnd`: when the claim applies, in UTC;
- `timezone` / `utcOffsetMinutes`: the civil-time interpretation used;
- `recurrence`: a structured daily or weekly local-time rule when present.

Relative phrases such as today, yesterday, tomorrow, and explicit local dates are
resolved deterministically against the case clock. Date-only ranges follow civil
midnight, so a DST transition day can be 23 or 25 hours. A nonexistent local wall
time during a spring transition is normalized forward by the zone rules. The
semantic model receives the same clock snapshot for classification but cannot
write its own timestamps.

### Card contract

A Timeline card has a typed kind, normalized declarative text, recorded and
effective times, civil timezone, provenance, consent, status, tier, scope,
structured `subject` / `predicate` / `value`, and an optional stable
`memory_key`. Supported producers use these kinds:

```text
fact  event  state  preference  task-preference
scene  observation  capture  tool-effect
```

Only active cards are eligible for recall or for an assistant claim such as
"I remember that." Questions, answer requests, greetings, and filler do not become
cards. An explicit memory statement is active immediately. An inferred reusable
preference remains pending until accepted.

An incomplete explicit request such as "remember a phone number" creates a
five-minute `MemoryDraft` in SQLite. The draft records its subject, predicate,
missing fields, language, source turns, and server-time expiry. A later turn can
refine or consume it only in the same participant and case and only when its
subject and predicate are compatible. Unrelated facts remain ordinary case cards.
The draft reaches `consumed` only after the compatible long-term card commits, and
survives an application restart until that terminal state or its real-time expiry.

One transcript may produce up to three atomic cards. Independent claims, such as
an object's location and a medication schedule, remain separate cards. Stable keys
are generated at the storage boundary from normalized entity and relation, for
example `contact:小李:phone`; a model-supplied generic key cannot merge two people.

A stable `memory_key` supersedes an older active value only in the exact same
scope and, for case scope, the same case. The old card remains available for audit.
Repeated event text remains separate because each occurrence has its own time.

### Scope

- `case`: visible only inside the owning participant's exact experiment case.
- `participant`: reusable across that participant's cases when the corresponding
  policy component is enabled.

Changing a profile or scope does not copy, merge, or delete existing cards.
Participant scope is therefore an explicit experimental treatment, not a neutral
storage choice. UI and API deletion mark cards inactive without rewriting the
append-only journal history.

### Current, short, and long views

The console snapshot includes active and pending cards so that unresolved capture
or confirmation state remains visible. Retrieval still accepts active cards only.

- `current`: cards whose occurrence time is within the most recent ten minutes by
  default.
- `short`: the working tier; active non-capture cards are consolidated after the
  default capacity of 40 is exceeded.
- `long`: explicit durable cards, pending durable preferences, and deterministic
  consolidation output.

Consolidation is a lossless deterministic promotion. Older short-term cards are
copied one-for-one into the long layer with the same text, entity, value, effective
time, timezone, recurrence, provenance, and importance; source IDs link the old
and promoted records. The old record becomes `consolidated`. This bounds the short
working set without summarizing away a phone number, date, or schedule. Promotion
runs outside the turn worker, is idempotent, is tracked per case, and is awaited
during service cleanup.

### Retrieval

Retrieval first enforces status, participant/case ownership, structured subject and
predicate, and effective-time overlap. Only then does it rank at most 20 auditable
candidates; at most five cards are injected into the current turn. A point-time
query uses an exact minute window, while a date-only query uses that civil day.

The built-in hashing encoder is deterministic and dependency-light. Its vector
score is ranking-only: a hash collision is not semantic evidence, so a candidate
also needs lexical evidence. Pure vector eligibility is available only when a real
external semantic encoder is injected. There is no vector database, knowledge
graph, timestamp-neighbor expansion, or LLM reranker in the default online path.

Retrieval evidence and `turn_context` are written to the case journal for audit but
are never replayed as user messages and never enter compaction summaries. The
provider receives one `current_turn_context` containing the exact current
transcript and this turn's selected evidence. Historical evidence therefore cannot
become the apparent latest user request after reconnect or compaction.

### Memory profiles

Profile IDs are retained for experiment compatibility:

| Profile | Participant cards/preferences | Case timeline cards |
| --- | --- | --- |
| `all-online` | on | on |
| `context-only` | off | off |
| `confirmed-preference` | on | off |
| `lexical-episodic` | off | on |
| `custom` | explicit settings | explicit settings |

`lexical-episodic` is a historical identifier, not the current algorithm name.
The append-only recovery journal and deterministic compaction remain enabled in
all profiles; they are controlled infrastructure, not behavioral-memory treatment.

Legacy `preferences.json` and `episodes.json` files are import sources only. New
writes go to TimelineMemory.

## Privacy And Evaluation

The Privacy panel and `/api/privacy` expose the provider disclosure, transport
boundary, retention policies, evaluation provenance, and account controls.
`/api/privacy/export` includes transcripts, intents/jobs/results, connector
references, journals, TimelineMemory, MemoryDraft, case/participant scope, and
real outcome evidence. `/api/privacy/delete` requires an explicit confirmation,
is idempotent, removes those records, and returns a counts-only deletion proof.

Retention is configured per account for transcript, observation, operational,
and memory data. Operational projections redact common Chinese/domain PII and reject raw
transcript, prompt, payload, and tool-argument metric labels. This is an
observability boundary, not a substitute for consent. Non-loopback deployments
must terminate TLS; deployed databases require an encrypted volume.

The online evidence ledger only accepts user acknowledgements, connector
receipts, or annotated perception sequences and always publishes a denominator.
The legacy intervention summary is a deterministic offline evaluator and is
labelled as such in the console and documentation.

## Skills And Tools

Bundled Skills live under `egoassist/skills/`. `--skills PATH` replaces the registry
with another collection.

```text
egoassist/skills/my-skill/
|-- SKILL.md
|-- skill.json
|-- tasks/*.json          # optional
`-- references/*.md      # optional
```

The provider initially receives only registered Skill names and descriptions. Full
instructions, task knowledge, and domain tools are disclosed after routing selects
one Skill. The realtime router can select one registered name, `none`, or
`ambiguous`; it cannot invent or activate an undeclared Skill through a tool call.

Tools are validated against the active Skill manifest. Undeclared tools,
parameters, handlers, and cross-Skill calls fail closed. A `tool-effect` memory card
is written only after a committed, non-error tool result explicitly reports
success. Model wording, a confirmation request, rejection, exception, or
`unsupported` result cannot create an effect card.

`set_timer` remains `unsupported` unless `EGOASSIST_TEMPORAL_TARGET` connects to a
Temporal server and the reminder worker starts. When connected, timer lifecycle,
retry, cancellation, and restart recovery belong to Temporal; the activity writes
one stable result to the durable ResultInbox before optional ntfy delivery. The
tool first returns `queued`, not `scheduled`, and a failed workflow becomes a
visible failed job/result rather than a success claim.

Realtime browser media uses LiveKit when `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and
`LIVEKIT_API_SECRET` are all configured and the room connection succeeds. The
browser publishes its microphone and camera as native tracks; the interaction
core subscribes and normalizes them for the existing provider bridge, and speech
is returned on one LiveKit audio track. Control, TurnInbox persistence, intent
attribution, TimelineMemory, MemoryDraft, and speech policy remain EgoAssist
responsibilities. A failed room connection is displayed as a bounded WebSocket
media fallback, never as a successful LiveKit connection.

Task references are declared in each Skill manifest and become visible only when
that Skill and a concrete task/checkpoint are current. The server resolves only
catalogued files beneath the Skill root, records a checksum and access receipt,
and authorizes every content request against the current case. PDF.js `6.2.108`
owns browser PDF rendering, page navigation, and zoom; EgoAssist owns task and
checkpoint selection. Images use the same authorization path and a native viewer.

Remote human help is `unconfigured` unless `EGOASSIST_HUMAN_HELP_WEBHOOK` is set.
The model cannot submit this request. The console first persists and displays the
exact purpose, message, current task context, data categories, and destination
origin. Only a second request with explicit user consent invokes the webhook with
a stable `Idempotency-Key`. Submitted, acknowledged, resolved, failed, and timed
out receipts are persisted and surfaced through Intent/Job/ResultInbox state.

## Configuration

Command-line options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--host` | `127.0.0.1` | HTTP/WebSocket bind address |
| `--port` | `8770` | HTTP/WebSocket port |
| `--open` | off | Schedule a browser launch |
| `--skills` | bundled Skills | Skill registry directory |
| `--active-skill` | none | Seed new cases with a Skill |
| `--memory-path` | `.egoassist/runtime.json` | Persistent-root anchor |
| `--model` | `qwen3.5-omni-flash-realtime` | Realtime dialogue model |
| `--voice` | `Ethan` | Provider voice |

Relevant environment settings:

```text
QWEN_API_KEY=...
# or DASHSCOPE_API_KEY=...

EGOASSIST_TURN_ROUTER_MODEL=qwen-flash
EGOASSIST_TURN_ROUTER_TIMEOUT=3.0
EGOASSIST_RESPONSE_CREATE_TIMEOUT=5
EGOASSIST_RESPONSE_FINISH_TIMEOUT=45

EGOASSIST_TEMPORAL_TARGET=127.0.0.1:7233
EGOASSIST_TEMPORAL_NAMESPACE=default
EGOASSIST_TEMPORAL_TASK_QUEUE=egoassist-reminders-v1
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
# Optional push in addition to the persistent ResultInbox:
EGOASSIST_NTFY_SERVER=https://ntfy.sh
EGOASSIST_NTFY_TOPIC=
EGOASSIST_NTFY_TOKEN=
EGOASSIST_HUMAN_HELP_WEBHOOK=
EGOASSIST_HUMAN_HELP_TOKEN=
EGOASSIST_HUMAN_HELP_TIMEOUT=8

EGOASSIST_CONTEXT_WINDOW=32768
EGOASSIST_CONTEXT_RESERVE=4096
EGOASSIST_CONTEXT_KEEP_RECENT=6000
EGOASSIST_CONTEXT_SUMMARY=3000
EGOASSIST_MAX_CASE_OWNERS=128
```

## Verification

Run the provider-neutral release gate:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\verify_egoassist.py
.\.venv\Scripts\python.exe -m compileall -q `
  egoassist run_egoassist.py run_egolife_benchmark.py
node --check egoassist\console\app.js
git diff --check
```

Additional checks:

```powershell
# General -> Skill -> exact task on one WebSocket
.\.venv\Scripts\python.exe scripts\check_example_skill_routing.py

# Real semantic-router matrix; consumes provider API access
.\.venv\Scripts\python.exe scripts\check_semantic_router.py

# Real provider and media/control stress check
.\.venv\Scripts\python.exe scripts\check_realtime.py `
  --exercise-seconds 18 --stress-controls --stress-media-gap

# Offline EgoLife visual-memory benchmark
.\.venv\Scripts\egoassist-egolife.exe --help
```

The deterministic suite covers persistence across application restart, participant
and case isolation, stale-turn cancellation, concurrent WebSocket ownership,
reconnect routing, historical-evidence exclusion, tool-result boundaries, memory
deletion, capture expiry, consolidation idempotency, and visual provenance. It does
not replace a final test with the intended browser hardware, network, and provider
account.

## Repository Layout

```text
egoassist/
|-- realtime/       # provider bridge, owner/turn control, routing, tasks, journal
|-- memory/         # TimelineMemory and offline visual adapters
|-- console/        # packaged research console
|-- skills/         # bundled replaceable Skills
|-- capture/        # offline stream and event segmentation
|-- retrieval/      # offline temporal localization
|-- backends/       # model adapters
`-- eval/           # evaluation metrics

tests/              # deterministic regression suite
scripts/            # release and real-provider checks
run_egoassist.py     # HTTP/WebSocket application
run_egolife_benchmark.py
```

## Deployment Boundary

```powershell
docker build -t egoassist .
docker run --rm -p 8770:8770 --env-file .env egoassist
```

The local participant login isolates research artifacts; it is not production
authentication. The server has no TLS, CSRF hardening, multi-host coordination, or
public deployment claim. Keep it on `127.0.0.1`, or place it behind a separately
reviewed authenticated TLS gateway.

Known capability boundaries:

- The model can still make ordinary perception or semantic errors. The runtime
  prevents known stale-turn and context-pollution paths; it cannot guarantee a
  correct answer.
- SQLite plus the memory-root lease supports one EgoAssist service process per
  memory root, not distributed active-active serving.
- The default online retriever is transparent and reproducible, but is not a
  learned semantic-memory system.
- Timers and reminders remain unavailable unless the Temporal worker starts;
  human help remains unavailable unless a real webhook connector is configured.
- Raw audio and camera frames are not retained by TimelineMemory.

## License

EgoAssist is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE)
for attribution.
