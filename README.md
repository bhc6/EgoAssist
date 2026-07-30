# EgoAssist

**Egocentric AI research base** — a minimal, reliable foundation for building
Pi-Agent / Claude-Code style assistants in the first-person embodied domain.

The goal is *大道至简*: a small, auditable core that anyone can fork and build on,
engineered to production-grade reliability (inspired by [Omi](https://github.com/BasedHardware/omi)).
The framework speaks [MCP](https://modelcontextprotocol.io) as its external-tool
protocol, ships a ReAct agent loop, and keeps memory, retrieval, and perception
behind typed protocol boundaries so each layer can be replaced independently.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   run_egoassist.py                  │  HTTP / WebSocket server
│  browser audio+camera  →  dialogue lane  →  speech  │
└────────────────────────┬────────────────────────────┘
                         │ authenticated turn
          ┌──────────────▼──────────────┐
          │        AgentLoop (ReAct)     │  observe → think → act
          │   SemanticTurnRouter         │  route → Skill/task
          └──────┬──────────────┬────────┘
                 │              │
        ┌────────▼───┐  ┌───────▼────────┐
        │  MCP Tools  │  │ SkillToolExec  │  external connectors + local tools
        └────────────┘  └───────┬────────┘
                                │
          ┌─────────────────────▼──────────────────────┐
          │               MemoryBackend                 │
          │  TimelineMemory (SQLite WAL, FTS5, FAISS)  │
          │  HierarchicalMemory  │  PreferenceMemory    │
          └─────────────────────────────────────────────┘
```

---

## Core Modules

| Layer | Path | What it does |
|---|---|---|
| **Memory** | `egoassist/memory/` | `TimelineMemory` — SQLite WAL, FTS5 full-text, FAISS vector index, v1→v2 in-place migration |
| **MemoryBackend** | `egoassist/memory/interface.py` | Unified `ingest_text` / `retrieve_text` protocol; swap backends without touching the agent |
| **Retrieval** | `egoassist/retrieval/` | `VectorIndex` protocol (FlatIndex / FAISSIndex), temporal localisation, semantic router |
| **MCP** | `egoassist/realtime/mcp_provider.py` | `MCPToolProvider` — stdio and HTTP/SSE transports; `EGOASSIST_MCP_SERVERS` env config |
| **AgentLoop** | `egoassist/realtime/agent_loop.py` | ReAct loop (observe → think → act); sync and async tool handlers; trajectory in `ContextHarness` |
| **Realtime** | `egoassist/realtime/` | Full-duplex provider bridge, turn worker, Skill/task routing, session journal |
| **Benchmarks** | `egoassist/eval/` | `EgoSchemaAdapter` (MCQ accuracy) and `Ego4DNLQAdapter` (R@1 IoU) |
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

### 1 — Plug in your MemoryBackend

```python
from egoassist.memory.interface import MemoryBackend

class MyVectorDB:
    def ingest_text(self, text, *, user_id, kind, metadata) -> str: ...
    def retrieve_text(self, query, *, user_id, limit=8) -> list[dict]: ...
```

Pass it to `TimelineMemory` or `HierarchicalMemory` via `MemoryBackend`.

### 2 — Attach MCP servers

```bash
# In .env — comma-separated  name:command  or  name:url
EGOASSIST_MCP_SERVERS=filesystem:npx -y @modelcontextprotocol/server-filesystem /tmp,custom:http://localhost:8000/mcp
```

```python
from egoassist.realtime.mcp_provider import providers_from_env, MCPToolProvider
from egoassist.realtime.tools import SkillToolExecutor

executor = SkillToolExecutor(...)
for provider in providers_from_env(os.environ["EGOASSIST_MCP_SERVERS"]):
    await provider.start()
    executor.attach_mcp_provider(provider)
```

### 3 — Run the AgentLoop

```python
from egoassist.realtime.agent_loop import AgentLoop

loop = AgentLoop(model="claude-opus-4-8", tool_executor=executor)
result = await loop.run(
    "What did I do yesterday?",
    system="You are an egocentric assistant.",
    memory_context=memory_context_string,
)
print(result.text)
```

### 4 — Write a Skill

```
egoassist/skills/my-domain/
├── SKILL.md          # system prompt
├── skill.json        # manifest: name, description, tools[], tasks[]
├── tasks/*.json
└── references/*.md
```

Bundled skills are the reference implementation in `egoassist/skills/`.

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

---

## Running Benchmarks

```python
from egoassist.eval.egoschema import EgoSchemaAdapter
from egoassist.eval.ego4d_nlq import Ego4DNLQAdapter

# EgoSchema — multiple-choice video QA
result = EgoSchemaAdapter("data/egoschema").run(my_qa_fn)
# → {"benchmark": "EgoSchema", "accuracy": 0.62, ...}

# Ego4D-NLQ — temporal grounding
result = Ego4DNLQAdapter("data/ego4d_nlq").run(my_localize_fn)
# → {"benchmark": "Ego4D-NLQ", "R@1_IoU0.3": 0.35, ...}
```

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

**`.env`**

```text
QWEN_API_KEY=...                          # or DASHSCOPE_API_KEY

# Turn routing
EGOASSIST_TURN_ROUTER_MODEL=qwen-flash
EGOASSIST_TURN_ROUTER_TIMEOUT=3.0

# Context window
EGOASSIST_CONTEXT_WINDOW=32768
EGOASSIST_CONTEXT_RESERVE=4096
EGOASSIST_MAX_CASE_OWNERS=128

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
python -m pytest                          # 200+ deterministic tests, no API key needed
python scripts/verify_egoassist.py        # release gate
python -m compileall -q egoassist run_egoassist.py
```

The deterministic suite covers: persistence across restart, participant/case
isolation, stale-turn cancellation, concurrent WebSocket ownership, reconnect
routing, tool-result boundaries, memory deletion, consolidation idempotency,
FTS5 migration, vector index, MCP provider, and agent loop.

---

## Repository Layout

```text
egoassist/
├── memory/         TimelineMemory, MemoryBackend protocol, adapters
├── retrieval/      VectorIndex (FAISS/Flat), temporal localisation, router
├── realtime/       AgentLoop, MCP provider, Skill/task/turn control, journal
├── eval/           EgoSchema and Ego4D-NLQ benchmark adapters
├── skills/         bundled replaceable Skills
├── capture/        offline stream segmentation
├── backends/       model adapters
├── product/        privacy, artifacts, human-help, telemetry
└── console/        packaged research UI

tests/              deterministic regression suite
scripts/            release and real-provider checks
run_egoassist.py    HTTP/WebSocket server entry point
```

---

## TimelineMemory

SQLite WAL, `memory_cards` table, FTS5 trigram full-text index, optional FAISS
ANN index. Schema v1→v2 in-place migration preserves all existing cards.

Card kinds: `fact event state preference task-preference scene observation capture tool-effect`

Memory tiers: `current` (last 10 min) → `short` (working set, 40 cards) →
`long` (durable; lossless deterministic promotion, no LLM summarisation).

Scope: `case` (per-experiment) or `participant` (cross-session, explicit opt-in).

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
