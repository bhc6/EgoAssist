# EgoAssist

**Egocentric AI research base** — a minimal, reliable foundation for building
Pi-Agent / Claude-Code style assistants in the first-person embodied domain.

The goal is *大道至简*: a small, auditable core that anyone can fork and build on,
engineered to production-grade reliability (inspired by [Omi](https://github.com/BasedHardware/omi)).
The framework speaks [MCP](https://modelcontextprotocol.io) as its external-tool
protocol, routes each turn through an explicit Skill/task state machine rather
than a free-running agent loop, and keeps memory, retrieval, and perception
behind typed boundaries so each layer can be replaced independently.

Latest real-provider audit: [memory latency and Skill workflows (2026-09-09)](docs/MEMORY_SKILLS_VALIDATION_2026-09-09.md), including failed trials, fixes, and the remaining cloud-latency limits.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   run_egoassist.py                  │  HTTP / WebSocket server
│  browser audio+camera  →  dialogue lane  →  speech  │
└────────────────────────┬────────────────────────────┘
                         │ authenticated turn
          ┌──────────────▼──────────────┐
          │      SemanticTurnRouter      │  one call → intents + Skill choice
          │            ↓                 │
          │        SkillRuntime          │  active Skill, task, checkpoints
          └──────┬──────────────┬────────┘
                 │              │
        ┌────────▼───┐  ┌───────▼────────┐
        │  MCP Tools  │  │ SkillToolExec  │  external connectors + local tools
        └────────────┘  └───────┬────────┘
                                │
          ┌─────────────────────▼──────────────────────┐
          │              TimelineMemory                 │
          │       SQLite WAL, FTS5 trigram, FAISS       │
          │  memory/adapters.py  │  HierarchicalMemory  │
          └─────────────────────────────────────────────┘
```

There is no autonomous think→act loop. What replaces it is deliberate: every
turn is classified once by `SemanticTurnRouter`, dispatched against an explicit
task/checkpoint state machine, and every context assembly is bounded by
`ContextHarness`. Nothing advances a task except a structured user confirmation.

---

## Core Modules

| Layer | Path | What it does |
|---|---|---|
| **Memory** | `egoassist/memory/timeline.py` | `TimelineMemory` — SQLite WAL, FTS5 trigram full-text, optional FAISS vector index, in-place schema migration to v3 |
| **Memory adapters** | `egoassist/memory/adapters.py` | Import external visual/text items into memory cards without the caller touching the store |
| **Retrieval** | `egoassist/retrieval/` | `VectorIndex` protocol (`FlatIndex` / `FAISSIndex`), temporal localisation, semantic router |
| **MCP** | `egoassist/realtime/mcp_provider.py` | `MCPToolProvider` — stdio and HTTP/SSE transports; `providers_from_env()` config parsing |
| **Context** | `egoassist/realtime/context_harness.py` | `ContextHarness` — token-bounded context assembly, retrieval injection, trajectory record |
| **Routing** | `egoassist/realtime/semantic.py`, `skills.py` | `SemanticTurnRouter` (one call → speech control, task control, preferences, Skill choice) and `SkillRuntime` |
| **Work** | `egoassist/realtime/work_ledger.py` | `CaseWorkLedger` — durable intents, jobs, and the result inbox |
| **Perception** | `egoassist/realtime/perception.py`, `ambient.py` | Checkpoint-bound frame validation ([ADR 005](docs/ADR_005_CHECKPOINT_BOUND_PERCEPTION.md)) and task-free ambient observation ([ADR 007](docs/ADR_007_AMBIENT_OBSERVATION.md)) |
| **Habits** | `egoassist/habits/` | von Mises circular time-of-day induction; observation-gated reminder decisions |
| **Abstention** | `egoassist/abstention/conformal.py` | `ConformalAbstention` — split-conformal threshold, risk–coverage curve |
| **Benchmarks** | `egoassist/eval/` | `real_metrics.py` (exact match, token F1, temporal IoU) and `metrics.py` (synthetic QA records) |
| **Product** | `egoassist/product/` | Privacy governance, artifact ports, human-help webhook, outcome ledger |

---

## Quick Start

```powershell
cd D:\DEMO\EgoAssist
Copy-Item .env.example .env          # set QWEN_API_KEY or DASHSCOPE_API_KEY
pip install -e ".[dev]"
python run_egoassist.py --open        # opens http://127.0.0.1:8770
```

Register a local identity, create an experiment case, and start a realtime session.

**Two servers on the same machine** — port *and* memory root must differ:

```powershell
python run_egoassist.py --port 8771 --memory-path .egoassist-8771/runtime.json
```

---

## Building on Top (Secondary Development)

EgoAssist is a *base layer*. The intended extension model:

### 1 — Feed your own data into memory

`TimelineMemory` is the store; `remember()` is the only write path, and every
card carries provenance, civil-time semantics, and a scope.

```python
from egoassist.memory.timeline import TimelineMemory

memory = TimelineMemory(".egoassist/timeline.db")
card = memory.remember(
    "厨房，早上八点，看到服药动作",
    user_id="participant-1",
    case_id="case-1",
    scope="case",                # case | participant
    kind="observation",          # fact event state preference task-preference
                                 # scene observation tool-effect capture legacy
    subject="participant-1", predicate="took-medication", value="kitchen",
)
cards = memory.cards(user_id="participant-1", kinds=["observation"])
```

For bulk external items use `egoassist.memory.adapters.import_visual_items()`
rather than writing to the database directly.

### 2 — Attach MCP servers

```python
import os
from egoassist.realtime.mcp_provider import providers_from_env
from egoassist.realtime.tools import SkillToolExecutor

executor = SkillToolExecutor()
for provider in providers_from_env(os.environ["EGOASSIST_MCP_SERVERS"]):
    await provider.start()
    tools = await provider.list_tools()
    # Bridge each MCP tool to a synchronous handler at your own async boundary:
    # SkillToolExecutor.execute() is deliberately synchronous.
    executor.register_handler("my-mcp-handler", my_sync_bridge(provider))
```

`EGOASSIST_MCP_SERVERS` is comma-separated `name:command` or `name:url`:

```bash
EGOASSIST_MCP_SERVERS=filesystem:npx -y @modelcontextprotocol/server-filesystem /tmp,custom:http://localhost:8000/mcp
```

MCP is a supported client transport, not yet wired into the default server
startup — `run_egoassist.py` does not call `providers_from_env()` for you.

### 3 — Write a Skill

```
egoassist/skills/my-domain/
├── SKILL.md          # system prompt
├── skill.json        # manifest: name, description, tools[], tasks[]
├── tasks/*.json
└── references/*.md
```

Bundled skills are the reference implementation in `egoassist/skills/`.

---

## Ambient Observation (off by default)

The elderly medication-adherence scenario needs the camera to notice a behaviour
nobody asked it to watch for. [ADR 005](docs/ADR_005_CHECKPOINT_BOUND_PERCEPTION.md)
drops any frame without an active task, so ambient observation is a **separate,
parallel contract** rather than a relaxation of that one — see
[ADR 007](docs/ADR_007_AMBIENT_OBSERVATION.md) for the full decision, including
what it makes genuinely weaker.

```bash
EGOASSIST_AMBIENT_MODE=1              # default 0; a default deployment is unchanged
EGOASSIST_AMBIENT_WATCHLIST=          # empty = built-in medication watchlist
EGOASSIST_AMBIENT_INTERVAL_SECONDS=20 # floor 5
```

Boundaries, in the order they apply:

- Off unless the deployment sets the flag, **and** the participant grants consent
  in the console. The consent id sits inside the frame binding, so revoking it
  invalidates frames already in flight.
- The watchlist is a finite, reviewable set of activities and places — kitchen,
  dining table, living room. No bedroom, no bathroom (Aria community guidelines).
- Absence is recorded as evidence (`observed=False`), which is what a missed-dose
  reminder rests on. A frame that fails to parse is *not* recorded as absence.
- A reminder requires **coverage today**: with no ambient frames since local
  midnight the system cannot tell a missed dose from glasses left on the charger,
  so it stays silent.
- Raw frames are never persisted, and ambient observation can neither advance a
  task nor speak on its own.
- While it runs, the console shows a top-level banner naming what is watched,
  where, how often, the latest result, and a revoke button.

---

## Optional Integrations

All optional — the base runs without any of them.

| Integration | Purpose | Configure |
|---|---|---|
| **LiveKit** | Realtime WebRTC media transport | `LIVEKIT_URL` + `LIVEKIT_API_KEY` + `LIVEKIT_API_SECRET` |
| **Temporal** | Durable reminder workflows | `EGOASSIST_TEMPORAL_TARGET=127.0.0.1:7233` |
| **OpenTelemetry** | Distributed tracing | install `opentelemetry-sdk`; no-op if absent |
| **FAISS** | Fast ANN vector search | install `faiss-cpu`; falls back to O(n) FlatIndex |
| **ntfy** | Push notifications | `EGOASSIST_NTFY_SERVER` + `EGOASSIST_NTFY_TOPIC` |
| **scipy** | von Mises habit fitting | optional; `egoassist/habits/` falls back to circular moments |

---

## Running Benchmarks

The real entry point is `run_egolife_benchmark.py`, against a downloaded
[EgoLife](https://egolife-ai.github.io/) subset. It runs the full pipeline —
captioning, retrieval, answer generation, temporal localisation — and applies
split-conformal abstention to the result.

```powershell
python run_egolife_benchmark.py `
  --data-dir data_real/egolife/A1_JAKE_DAY1 `
  --output-dir outputs/my_run `
  --max-videos 4 --fps 0.5 --alpha 0.1 --calibration-ratio 0.5
```

| Flag | Default | Description |
|---|---|---|
| `--data-dir` | *required* | Downloaded EgoLife day directory |
| `--max-videos` | all | Cap videos; **fewer than 2 yields no held-out test split** |
| `--fps` | `0.5` | Frame sampling rate for captioning |
| `--alpha` | `0.1` | Conformal target risk |
| `--calibration-ratio` | `0.5` | Fraction of videos used for calibration |
| `--backend` | `local` | `local` (Qwen3-VL on-device) or `dashscope` |
| `--model-id` / `--api-model` | Qwen3-VL-2B / `qwen3-vl-plus` | Model selection |
| `--precision` | `fp16` | `fp16` or `4bit` for the local backend |

Outputs are `results.jsonl` (per question) and `summary.json` (aggregate).

**Read the summary honestly.** With a single video every question lands in the
calibration set, so `n_test == 0` and every held-out metric is `null` — not
`0.0`. `split_status` says which case you are in:

| `split_status` | Meaning |
|---|---|
| `held-out-test-split` | Real measurement; `token_f1` etc. are meaningful |
| `no-test-split` | Fewer than 2 videos; held-out metrics are `null`, use the `all_sample_*` fields |
| `no-rows` | Nothing ran |

The `all_sample_*` fields are computed over every question including the
calibration set. They are real numbers but **not** a held-out measurement.

Existing runs in `outputs/` predate this fix and still show the misleading
`token_f1: 0.0` with `n_test: 0`; they also record a `mean_query_seconds` of
191 s (local) to 260 s (cloud) per question, which is why latency is named
openly as unsolved below rather than buried.

`egoassist/eval/real_metrics.py` holds the metric primitives
(`exact_match`, `token_f1`, `temporal_iou`) if you want to score your own runs.

---

## Configuration Reference

**CLI**

| Flag | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8770` | HTTP/WebSocket port |
| `--open` | off | Launch browser on start |
| `--skills` | bundled | Skill registry directory |
| `--active-skill` | none | Seed new cases with a Skill |
| `--memory-path` | `.egoassist/runtime.json` | Persistent-root anchor |
| `--model` | `qwen3.5-omni-flash-realtime` | Realtime dialogue model |
| `--voice` | `Ethan` | Provider voice |

**`.env`** — see `.env.example` for the annotated copy.

```text
QWEN_API_KEY=...                          # or DASHSCOPE_API_KEY

# Turn routing
EGOASSIST_TURN_ROUTER_MODEL=qwen-flash
EGOASSIST_TURN_ROUTER_TIMEOUT=3.0

# Context window
EGOASSIST_CONTEXT_WINDOW=32768
EGOASSIST_CONTEXT_RESERVE=4096
EGOASSIST_MAX_CASE_OWNERS=128

# Optional: ambient observation (ADR 007), off by default
EGOASSIST_AMBIENT_MODE=0
EGOASSIST_AMBIENT_WATCHLIST=
EGOASSIST_AMBIENT_INTERVAL_SECONDS=20

# Optional: LiveKit
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=

# Optional: Temporal reminders
EGOASSIST_TEMPORAL_TARGET=127.0.0.1:7233
EGOASSIST_TEMPORAL_NAMESPACE=default

# Optional: Human-help webhook
EGOASSIST_HUMAN_HELP_WEBHOOK=
EGOASSIST_HUMAN_HELP_TOKEN=

# Optional: ntfy push
EGOASSIST_NTFY_SERVER=https://ntfy.sh
EGOASSIST_NTFY_TOPIC=
```

---

## Testing

```powershell
python -m pytest                          # 332 deterministic tests, no API key needed
python scripts/verify_egoassist.py        # release gate
python -m compileall -q egoassist run_egoassist.py
```

The deterministic suite covers: persistence across restart, participant/case
isolation, stale-turn cancellation, concurrent WebSocket ownership, reconnect
routing, tool-result boundaries, memory deletion, consolidation idempotency,
FTS5 migration, vector index, MCP provider, checkpoint-bound perception,
ambient observation and consent revocation, habit induction across midnight, and
privacy export/deletion.

---

## Repository Layout

```text
egoassist/
├── memory/         TimelineMemory, adapters, temporal semantics, snapshots
├── retrieval/      VectorIndex (FAISS/Flat), temporal localisation, router
├── realtime/       MCP provider, Skill/task/turn control, perception, journal
├── habits/         circular-time habit induction, observation→reminder bridge
├── abstention/     split-conformal abstention
├── eval/           metric primitives (exact match, token F1, temporal IoU)
├── qa/             question-answering pipelines
├── skills/         bundled replaceable Skills
├── capture/        offline stream segmentation
├── backends/       model adapters
├── product/        privacy, artifacts, human-help, telemetry
└── console/        packaged research UI

docs/               architecture decision records
tests/              deterministic regression suite
scripts/            release and real-provider checks
run_egoassist.py    HTTP/WebSocket server entry point
run_egolife_benchmark.py  EgoLife evaluation entry point
```

---

## TimelineMemory

SQLite WAL, `memory_cards` table, FTS5 trigram full-text index, optional FAISS
ANN index. In-place migration from schema v1/v2 to v3 preserves all existing cards.

Card kinds: `fact event state preference task-preference scene observation tool-effect capture legacy`

Persisted tiers are `short` (working set) and `long` (durable; lossless
deterministic promotion, no LLM summarisation). Retrieval additionally surfaces
a derived `current` layer for cards inside the recency window.

Scope: `case` (per-experiment) or `participant` (cross-session, explicit opt-in).

---

## Known Limitations

Named here rather than left for a reader to discover:

- **Latency is not glasses-grade.** Measured end-to-end question latency is
  95–191 s on-device and 254–260 s via cloud APIs. A wearable assistant needs
  well under 2 s. Closing that gap is architectural work (on-device
  quantisation, smaller models, a shorter retrieval path), not configuration.
- **No Aria SDK integration.** `projectaria_tools` does not support Windows and
  the hardware is not in hand; USB passthrough under WSL is unverified. The
  browser camera path is the only live input today.
- **Held-out benchmark numbers are still thin.** The runs committed under
  `outputs/` used a single video each, so they carry no test split at all.
- **MCP is not auto-wired.** The client works; the default server startup does
  not attach providers.
- **The participant login is a research identity store**, not production
  authentication.

---

## Deployment

```powershell
docker build -t egoassist .
docker run --rm -p 8770:8770 --env-file .env egoassist
```

> The built-in participant login is a research identity store, not production
> authentication. Keep the server on `127.0.0.1` or place it behind a reviewed
> TLS gateway. SQLite + the memory-root lease supports one process per memory
> root, not distributed active-active serving.

---

## License

[Apache 2.0](LICENSE) — see [NOTICE](NOTICE) for attribution.
