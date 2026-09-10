# Hardcoding Refactor

Scope: eight audited issues, with source evidence and real provider validation.

## Plan

- [x] 1. Replace domain-specific memory fallback with durable raw-input recovery.
- [x] 2. Remove keyword gates from semantic memory decisions.
- [x] 3. Separate speaker attribution from command authority.
- [x] 4. Remove domain-specific relation and visual prompt prescriptions.
- [x] 5. Preserve compound memory/intents and support bounded iterative tools.
- [x] 6. Separate live visual freshness from historical evidence storage.
- [x] 7. Paginate complete model audit events and preserve frontend expansion.
- [x] 8. Configure and expose the actual ASR route and limits.
- [x] Run regression tests, real Qwen tests, browser checks and record outcomes.

## Invariants

Original input and media timestamps remain authoritative. Unknown speakers remain
unknown. Background recovery must not replay tools or task controls. Tool effects
require durable receipts. Resource exhaustion must remain visible. No test result
may be reported as real hardware validation without real hardware.

## Implementation

Memory failure retains the source transcript in the journal and queues
memory.recovery.job before interaction handling can return early. The worker uses
the original clock and source ID, checkpoints the validated model decision before
writing, and uses deterministic card IDs. It never applies recovered tool calls or
task controls. Recovery persists across restart, has bounded attempts and supports
explicit retry in the audit view. Late recovered facts do not supersede newer
facts automatically. Drafts retain source evidence and generic open capture uses
the memory.value workflow slot.

The same semantic command can execute from text or an unregistered voice.
Background speech still requires an explicit addressee decision; unknown voice
identity remains unknown in source metadata. Acoustic analysis no longer blocks
the foreground classifier. The microphone protocol requires interaction fields
and allows the existing bounded retry when the model omits them.

Memory events retain the full source note by default. Event and atom arrays may
coexist; the old 3-memory/4-intent caps and downstream slices are gone, as are
500/1000-character truncations of source memory. JSON schemas, time anchors and
source evidence checks remain. Model/context/request limits still exist.

Tools receive actual tool-role receipts and can choose dependent calls in later
rounds. Per-request call/round limits come from realtime/settings.py and env.
Repeated operation signatures reuse receipts within the run. Reused call IDs with
different arguments are rejected. Budget exhaustion is journaled with pending
calls and shown in the UI; continuing beyond budget requires another request.
This is a bounded tool loop, not an unbounded autonomous workflow engine.

Ambient history accepts correctly bound delayed frames at capture time. Live
clarifications still require the configurable live freshness deadline. Task-step
transition validation retains its stricter freshness rule. ASR model, concurrency
and timeout are configurable and visible in audit configuration.

Audit uses cursor pagination and discovers model-bearing events plus perception,
tool and recovery families. Full payloads remain in the original journal; browser
polling preserves opened details and merges pages.

## Validation

Regression: outputs/hardcoding-regression-final.xml, 446 tests passed.
Python compilation, JavaScript syntax and git diff checks passed.

All paths below are under outputs/runs/; provider calls are real Qwen.

| Run | Result |
| --- | --- |
| hardcoding-real-tools-first | Real README bytes, one execution, realtime answer: 2803.9 ms. Two selector calls: 931.7/719.6 ms. Browser artifact/refresh passed. |
| hardcoding-real-integrated-third | Negative memory 2767.2 ms, six-item note 2402.8 ms, correct negative recall 3617.8 ms. Dependent tools and desktop/mobile audit passed. |
| hardcoding-real-addressee-contract | 8/8: unknown voice direct requests, quiet, third-party/quoted speech and explicit memory. Text fixtures test semantics, not ASR. |
| hardcoding-real-ambient | Real EgoLife video frames: 1947.8/1618.2/1692.4 ms; actual media hashes, visual memory, refresh and daily report passed (report 6596.4 ms). |
| hardcoding-real-recovery-mixed | Restored queued source, actual Qwen, original date and idempotency passed: 1911.4 ms. Running-job state was injected, provider was not mocked. |
| hardcoding-real-social | Two cross-session social notes, correct yesterday/discussed-before-yesterday recall. First audio 2866.6 ms, full answer 4271.6 ms. |
| hardcoding-real-voice-continuation | Real TTS -> PCM -> realtime ASR -> draft continuation -> participant memory passed. 4800 ms source audio, 1791.8 ms from finished transmission to memory detection. Dependent tools 3747.1 ms. Audit paging, expansion, refresh, 1440/390 screenshots passed. |

## Failure Evidence And Limits

Failed runs remain intact: hardcoding-real-integrated-first (incorrect test
completion condition), integrated-second (compound model timeout and missing
early-return recovery submission), integrated-voice (natural save intent scoped
as observed), integrated-final (ASR split did not inherit draft), real-addressee
(missing interaction), real-recovery (mixed atoms/events rejected).
Names in this paragraph have the hardcoding- prefix as above.

The final voice run still hit two 4-second foreground timeouts on the long note.
The background worker completed it; the UI exposed the queued/recovered state.
That sample took about 18.9 seconds from submission to persisted recovery:
8.1 seconds foreground, then queue delay and a 5.4-second recovery call.
This is successful recovery, not a guarantee of sub-3-second memory latency.
Natural intent classification and ASR still vary. No real glasses, simultaneous
speaker identity accuracy, or physical speaker/microphone latency was measured.
Weekly/monthly report generation was not rerun in this change; daily shared
visual-source behavior was tested. Historical failures remain auditable.

## Running Demo

The updated server is available at http://127.0.0.1:8772 using
outputs/hardcoding-demo/runtime.json. HTTP returned 200. Existing servers and
their data were not restarted. Startup stdout/stderr are retained under outputs.
The local process-command-line inspection was rejected by automatic approval
review, so no existing process was selected for restart.
