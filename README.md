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

The default startup mode is general and unloaded:

```text
activeSkill = null
task        = null
knowledge   = none
```

As a bundled example, saying that you want to cook activates `kitchen-coach` but does not silently choose a recipe. Naming fruit salad activates its task. Unknown or ambiguous input remains in general mode.

## Runtime Flow

```text
browser microphone + latest camera frame
                  |
                  v
       Qwen Omni realtime bridge
                  |
       +----------+------------------+
       |                             |
 input transcript             text-only visual gate
       |                             |
       v                      scene summary + speak gate
 auditable intent router             |
       |                       speak=false: no audio
       v                       speak=true: scheduler
 optional Skill / task               |
       +------ memory + policy -------+
                  |
          interruptible speech
```

The console visualizes observable artifacts, not private chain-of-thought:

- media arrival, coalescing, and browser audio settings;
- input/output channel attribution and possible residual echo;
- intent candidates, evidence, scores, rejection reasons, and activation;
- structured observations and checkpoint hypotheses;
- loaded knowledge source and claim boundary;
- preference and episodic retrieval candidates;
- intervention policy, response queue, cancellation, and provider IDs;
- prompt hash, causal parent IDs, latency, and raw event JSON.

## Full-Duplex Attribution

The microphone remains active while the assistant speaks. Browser capture requests echo cancellation, noise suppression, and automatic gain control. Protocol events keep the two logical channels separate:

- user: `conversation.item.input_audio_transcription.completed`;
- assistant: `response.audio_transcript.*`.

Recent assistant output is compared with new input transcription to flag likely residual echo. The flag is observation-only: input is not dropped because a user may intentionally repeat the assistant. This is not biometric speaker diarization, and the UI states that boundary explicitly.

Turn-taking is Skill-configurable:

- `immediate`: stop ordinary guidance on user speech;
- `finish-clause`: finish the current short clause, then stop;
- `protected`: allow a bounded emergency utterance while input capture continues.

## Silence And Proactive Interaction

Periodic vision checks request text-only structured output:

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

Malformed, low-value, and meta-silence responses fail closed. A `speak=false` response cannot create spoken transcript or audio events. The first scene summary creates an episodic anchor; later summaries are persisted only when `salient_change=true`.

## Memory Boundaries

EgoAssist exposes distinct memory claims:

- working context: current realtime session and structured events;
- preference memory: explicit candidate, user confirmation, atomic persistence, exact task/checkpoint lookup, and one-shot reuse;
- realtime episodic memory: persistent scene/dialogue summaries with source events and auditable term-overlap retrieval; raw audio and frames are not retained;
- hierarchical visual memory: the original frame-segment, embedding, consolidation, temporal localization, and abstention pipeline used by the offline EgoLife benchmark;
- knowledge memory: no domain injection in general mode; static Skill/task injection after activation.

The realtime summary adapter and offline hierarchical visual backend intentionally share a conceptual contract without forcing a second visual model into the latency-sensitive WebSocket loop. Future research can replace the summary index with the hierarchical backend and evaluate the trade-off explicitly.

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

Tool names, confirmation policy, argument schema/defaults, adapter type, and local result wording are declared in `skill.json`. The core only validates the active Skill contract and dispatches a registered handler; the browser derives arguments from the same schema. Undeclared tools, parameters, and handlers fail closed.

## Verification

```powershell
# Deterministic tests and release checks
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\verify_egoassist.py

# Bundled example on one WebSocket: General -> Skill only -> exact task
.\.venv\Scripts\python.exe scripts\check_example_skill_routing.py

# Real provider, media ordering, controls, and cancellation races
.\.venv\Scripts\python.exe scripts\check_realtime.py `
  --exercise-seconds 18 --stress-controls --stress-media-gap

# Offline EgoLife hierarchical-memory benchmark
.\.venv\Scripts\egoassist-egolife.exe --help
```

Expected baseline: `77 passed`.

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

The service has no authentication or TLS. Bind it to `127.0.0.1` for local use or place it behind an authenticated gateway. Benchmark reports should distinguish baseline behavior from new methods and record model versions, Skill versions, latency, failures, abstentions, memory retention settings, and routing decisions.
