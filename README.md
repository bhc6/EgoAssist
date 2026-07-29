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

## Memory Boundaries

EgoAssist exposes distinct memory claims:

- working context: current realtime session and structured events;
- preference memory: explicit candidate, user confirmation, atomic persistence, exact task/checkpoint lookup, and one-shot reuse;
- realtime episodic memory: persistent scene/dialogue summaries with source events, auditable term overlap, and timestamp-neighbor expansion for conversational references; raw audio and frames are not retained;
- hierarchical visual memory: the original frame-segment, embedding, consolidation, temporal localization, and abstention pipeline used by the offline EgoLife benchmark;
- knowledge memory: no domain injection in general mode; static Skill/task injection after activation.

The behavioral realtime memory plugins are controlled per experiment case. The default `all-online` profile enables confirmed preferences at participant scope and lexical episodic memory at case scope. The control panel can select a named profile or independently change each plugin and its scope:

| Profile | Context journal | Confirmed preference | Lexical episodic |
| --- | --- | --- | --- |
| `all-online` (default) | required | on, participant | on, case |
| `context-only` | required | off | off |
| `confirmed-preference` | required | on, participant | off |
| `lexical-episodic` | required | off | on, case |
| `custom` | required | explicit | explicit |

Disabling a plugin gates its persistence, retrieval, and injection without deleting existing records. Case-scoped preference and episodic stores are physically partitioned so capacity eviction cannot leak across cases; participant scope uses a separate long-term store. The append-only context journal and deterministic compaction are recovery/audit infrastructure, not a behavioral memory treatment, and therefore remain enabled in every profile. The selected policy is versioned in SQLite, returned in runtime state, and appended to the case journal on every change. Changes apply to subsequent turns; a clean benchmark should set the policy before collecting a case rather than switch it mid-case.

Selected realtime episodes are injected into the same user turn before response creation. The injected block includes episode ID, timestamp, source, score, and summary; the model is instructed to use only relevant evidence and abstain when evidence is insufficient. The UI reports `context injection`, not causal generation impact: injection is observable, while proving that an output changed requires a counterfactual experiment.

The realtime summary adapter and offline hierarchical visual backend intentionally share a conceptual contract without forcing a second visual model into the latency-sensitive dialogue loop. The hierarchical backend is an offline research method, not a realtime control-panel plugin. Future research can implement that adapter and evaluate the trade-off explicitly instead of presenting an unavailable toggle.

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
