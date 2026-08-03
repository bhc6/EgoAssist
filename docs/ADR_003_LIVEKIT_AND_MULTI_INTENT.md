# ADR 003: LiveKit Media and EgoAssist Multi-Intent Ownership

Date: 2026-07-30
Status: Accepted and implemented in M3

## Decision

LiveKit owns browser room membership, native audio/video transport, congestion
control, reconnect, and the assistant output audio track. The existing Omni
bridge remains a provider adapter behind `InteractionTransport`.

EgoAssist remains the only owner of completed transcript persistence, typed
intent decomposition, foreground focus, Temporal job creation, ResultInbox,
tool attribution, speech arbitration, and the memory/safety layer. In
particular, `TimelineMemory`, `MemoryDraft`, participant/case scope, temporal
semantics, provenance, conflict handling, retrieval injection, and deletion are
not represented in or delegated to LiveKit room state.

The realtime session layer (`run_egoassist.py`) accesses memory exclusively
through `RealtimeEpisodeMemory` — a single unified object that combines BM25
episodic retrieval with task-preference operations via internal delegation to
`PreferenceMemory`. Both capabilities share the same `TimelineMemory` database;
the LiveKit integration layer sees one object, not two.

One classified turn may contain up to four typed intent candidates. A relative
reminder is accepted only with a validated duration and a live Temporal worker.
It becomes a durable reminder intent/job before the model acknowledgement. An
immediate question in the same transcript receives a separate foreground
intent/job. Completing that response cannot complete or cancel the reminder.
An incomplete explicit memory request remains a waiting-input intent backed by
the original EgoAssist MemoryDraft transaction.

## Failure Behavior

- Missing LiveKit configuration exposes `websocket-fallback`, not LiveKit.
- Browser or server room connection failure activates the bounded legacy media
  path and records explicit media gaps under backpressure.
- Short-lived browser tokens are authenticated, room-scoped, and never disclose
  the API secret.
- Topic changes alter foreground focus only. Accepted Temporal jobs and
  MemoryDraft transactions survive disconnect and reconnect.
- The existing WebSocket path remains until deployment black-box monitoring
  demonstrates LiveKit reliability; it is a feature-flagged fallback, not a
  second source of conversation truth.

## Verification

The M3 suite covers token grants, media event normalization, native assistant
audio, barge-in queue clearing, backpressure gaps, browser fallback, typed
decomposition, and a Temporal time-skipping scenario combining foreground
dialogue, a reminder, a waiting-input MemoryDraft, disconnect, trigger, and
reconnect without result cross-attribution.
