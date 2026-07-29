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

## Highlights

- Full-duplex browser audio with camera context and natural barge-in.
- General mode plus progressively disclosed, replaceable Skills.
- One ordered turn worker and one runtime owner per experiment case.
- Reconnectable, append-only case journals with bounded context compaction.
- One persistent SQLite TimelineMemory for facts, events, state, preferences,
  observations, scenes, captures, and successful tool effects.
- Explicit participant and case scopes with no implicit cross-case recall.
- Separate dialogue and perception provider sessions.
- Observable routing, memory provenance, response scheduling, tool execution, and
  failure states in the research console.

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

                 periodic latest frame -> isolated perception lane
                                      |
                      validated structured gate decision
                                      |
                  optional proactive utterance on dialogue lane
```

The dialogue lane owns user audio, ordered camera context, and user-visible
responses. The perception lane receives no microphone stream and has no direct
browser audio route. A validated `speak=true` gate may enqueue a separate proactive
utterance; malformed, stale, or unattributed output fails closed.

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

```text
<memory-root>/
|-- identity.sqlite3
|-- timeline.sqlite3
|-- .egoassist-server.lock
`-- users/<user-id>/sessions/<case-id>.jsonl
```

### Card contract

A Timeline card has a typed kind, normalized declarative text, occurrence and
creation times, provenance, consent, status, tier, scope, and optional stable
`memory_key`. Supported producers use these kinds:

```text
fact  event  state  preference  task-preference
scene  observation  capture  tool-effect
```

Only active cards are eligible for recall or for an assistant claim such as
"I remember that." Questions, answer requests, greetings, and filler do not become
cards. An explicit memory statement is active immediately. An inferred reusable
preference remains pending until accepted.

"Remember this value" without a value creates a five-minute pending capture. Only
the next classified value in the same participant and case can consume it. Capture
is consumed after the new card is successfully committed.

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

Consolidation groups older short-term cards by time block and kind, records source
card IDs, and uses deterministic IDs so retries are idempotent. It runs outside the
turn worker but is tracked per case and awaited during service cleanup.

### Retrieval

Retrieval first enforces status and ownership, routes explicit time language to
eligible layers, and then ranks at most 20 auditable candidates. At most five cards
are injected into the current turn.

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

The bundled `set_timer` adapter currently returns `unsupported`. It can compute a
relative deadline but there is no persistent alarm-delivery scheduler, so EgoAssist
must not claim that a timer or reminder was created.

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
- Timers and reminders remain unsupported until a durable scheduler and delivery
  channel are implemented.
- Raw audio and camera frames are not retained by TimelineMemory.

## License

EgoAssist is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE)
for attribution.
