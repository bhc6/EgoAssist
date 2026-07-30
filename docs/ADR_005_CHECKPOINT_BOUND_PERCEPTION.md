# ADR 005: Checkpoint-Bound Realtime Perception

Date: 2026-07-30
Status: Accepted and implemented in M5

## Decision

The existing isolated provider perception lane remains the realtime visual
inference transport. EgoAssist now owns a typed policy boundary around it. Every
submitted frame carries an immutable `PerceptionFrameContext` containing the
user and case, Skill, task ID and version, task provenance, current checkpoint,
checkpoint epoch, direct allowed transitions, completion criteria, risk, source
reference, authorized artifact checksums, frame ID, and capture time.

The provider returns a `PerceptionHypothesis`, never a completion command. The
policy accepts it only while the frame is fresh and the complete captured
binding still matches the active task. Unknown task steps, paths outside the
captured direct transitions, unrelated frames, future timestamps, stale frames,
and results from a changed task/checkpoint/epoch fail closed.

Accepted output becomes `ObservationEvidence` in the product database and an
auditable task observation in the case journal. Raw frames are not persisted.
The evidence is also eligible for the existing case-scoped episodic memory path,
subject to the user's memory policy. It does not create a second memory store or
replace TimelineMemory.

## Advancement And Speech Policy

- A visual completion hypothesis never calls `confirm_step()`.
- Distinct fresh frames may establish temporal consistency for display and a
  confirmation request; even consistent evidence cannot advance the checkpoint.
- Only the existing structured user-confirmation intent advances the task.
- Fresh task-bound hazards may request protected speech. A late result from a
  changed task, checkpoint, epoch, or case cannot speak.
- Direct path deviation may request clarification. A checkpoint outside the
  allowed path is rejected rather than normalized into the current task.
- The single dialogue speech scheduler remains the only user-visible audio
  writer.

## Explainability And Failure Behavior

The console displays the binding, allowed path, checkpoint epoch, frame ID and
age, visible evidence, consistency count, and latest rejection. Manual research
observations use the same policy and are explicitly labelled external evidence.
Malformed provider output and missing task context remain audit-only failures.

The provider session may reconnect or coalesce pending frames, but an in-flight
frame keeps its original immutable context. A new frame may replace only a
pending frame; it cannot rewrite the context of active inference.

## Verification

Annotated sequence tests cover stale frames, task/checkpoint changes during
inference, unrelated frames, allowed and forbidden paths, one-frame completion,
repeated independent evidence, explicit user confirmation, hazard provenance,
product-database persistence, and the immutable context carried by the realtime
lane. HTTP manual observations and the existing task/realtime suites exercise
the same validation and compatibility boundaries.

This milestone preserves EgoAssist's primary innovation: TimelineMemory,
MemoryDraft, participant/case isolation, provenance, temporal semantics,
conflict handling, retrieval injection, and user-controlled deletion remain the
owned memory and interaction-safety layer above the mature realtime foundation.
