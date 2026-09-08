# ADR 007: Ambient Observation Without A User-Initiated Task

Date: 2026-09-07
Status: Accepted; shipped disabled by default

## Context

[ADR 005](ADR_005_CHECKPOINT_BOUND_PERCEPTION.md) binds every perception frame
to an active task: no Skill, no task, no checkpoint means the frame is dropped
before it reaches a provider. That is a deliberate, accepted safety property,
not an oversight.

It also makes the product's central scenario impossible. MEMO's value is
observation-gated reminding — remind only when the behaviour has *not* been
seen, cancel the moment it is. An elderly participant does not start a task
called "take my medication" and then take it; the whole point is that nobody
has to remember to tell the system anything. Under ADR 005 the camera is blind
exactly when the product needs it to be watching.

## Decision

Add a second, parallel perception contract for task-free observation. Do not
relax the task contract.

- `AmbientFrameContext` ([contracts.py:163](../egoassist/product/contracts.py#L163))
  is a separate model with `schema_version = "ambient-context.v1"`
  ([ambient.py:27](../egoassist/realtime/ambient.py#L27)), validated by a
  separate `validate_ambient_result()`
  ([perception.py:299](../egoassist/realtime/perception.py#L299)). The task
  validator is untouched, byte for byte.
- Both contracts inherit `extra="forbid"` and carry a `Literal`
  discriminator, so feeding an ambient payload to the task validator raises
  rather than degrading, and vice versa.
- Observation rows are namespaced `task_id = f"ambient:{watchlist_id}"`. The
  `TaskSpec` id grammar (`[a-z0-9][a-z0-9_-]{1,63}`) admits no colon, so the two
  id spaces are provably disjoint. No schema change, no migration.
- Observation targets come from an `AmbientWatchList`
  ([ambient.py:64](../egoassist/realtime/ambient.py#L64)) — an explicit,
  finite set of activities and locations. Anything outside it is rejected
  (`location-outside-watchlist`), so the watched scope is a reviewable
  artifact, not a prompt.

### Absence is positive evidence

The task lane treats an off-task frame as a rejection (`unrelated-frame`). The
ambient lane must do the opposite: "looked, did not see the behaviour" is the
evidence a missed-dose reminder rests on. Such a frame is stored as
`observed=False, activity_id="none-observed"`. There is no `unrelated` field and
no completion concept in the ambient contract — an ambient frame cannot advance
anything, so there is nothing for a provider to claim.

A provider that returns `observed=False` alongside a concrete activity id, or
`observed=True` with none, is contradicting itself; both directions are rejected
as `contradictory-absence-claim`. A frame that fails to parse is *not* recorded
as absence — it was never assessed, and recording it would fabricate coverage.

### Coverage versus blindness

This is the distinction the reminder logic lives or dies on. A `none-observed`
row is proof the camera watched and saw nothing. *No row at all* means nothing
was watched — glasses on the charger, battery flat, consent revoked. Conflating
the two turns a dead battery into a missed-dose alert, which is precisely the
false positive the project charter's §6.4 (`MEMO-立项书.md`, 主动提醒逻辑 —
抗警报疲劳) forbids.

So `ambient_reminder_decision()` requires coverage today before it will remind:
with zero ambient frames since local midnight it returns
`no-observation-coverage-today` and stays silent.

## What is preserved from ADR 005

- Frame freshness (`MAX_FRAME_AGE_MS`), future-timestamp rejection, and the
  immutable binding compared field by field on return.
- Raw frames are never persisted (`rawMediaPersisted: False`).
- Every rejection is auditable. Ambient has no `task` object to hold
  `record_perception_rejection`, so rejections go to the case journal as
  `ambient_observation` events instead.
- Ambient observation cannot advance a checkpoint, cannot complete anything, and
  cannot speak. `session_ambient_result()` returns `"speech": None`
  unconditionally; whether to say anything is the habit bridge's decision,
  routed through the existing reminder workflow and the single speech scheduler.

## What is genuinely weaker

Stated plainly, because it is the cost of this decision:

**A frame can now reach a model provider with no user-initiated task.** Under
ADR 005 the user's own action was the gate. Under ADR 007 the gates are a
deployment flag, a per-participant consent grant, a finite watchlist, and a
sampling interval — all real, none of them the user's decision in the moment.

Mitigations, in the order they apply:

1. `EGOASSIST_AMBIENT_MODE` defaults **off**. A default deployment behaves
   exactly as it did before this ADR, and `/api/privacy` does not even mention
   an ambient provider.
2. Consent is per participant, separately granted, and revocable. The consent id
   sits *inside* the frame binding tuple, so revocation invalidates frames
   already in flight — it cannot be raced.
3. The watchlist authorizes kitchen, dining table, and living room. No bedroom,
   no bathroom (Aria community guidelines).
4. Sampling is interval-based (default 20 s, floor 5 s), not continuous.
5. A malformed watchlist disables ambient rather than falling back to defaults.
6. While ambient is live the console shows a top-level banner naming what is
   watched, where, how often, and the latest result — with a revoke button. It
   is not a collapsible panel; ambient must not be invisible.

## Habit induction

`egoassist/habits/` turns accepted observations into habits and reminder
decisions. Times are fitted with von Mises circular statistics so a 23:50 habit
averages to midnight rather than noon, and in **civil** time via `ClockContext`
— an 08:00 Beijing habit induced on UTC timestamps would be learnt as 00:00 and
the reminder would fire in the middle of the night.

`should_remind()` fires only *after* the expected window has elapsed without a
sighting, and stops after `lookback_min` (default 12 h): half a day late, the
moment has passed and nagging does not help. The elapsed comparison is circular,
because a window ending after 24:00 exceeds 1440 minutes and a plain
`now >= window_end` never fires.

## Verification

The claim that the task lane is unchanged rests on structure, not on testing:
a separate context model and a separate validator function are *incapable* of
weakening the task path, because the task path does not call them.

Tests pin the rest. `tests/test_ambient_perception.py` covers absence as
evidence, unauthorized locations, malformed output not becoming false absence
(both directions), consent revocation invalidating an in-flight frame, frame-id
dedup, the task validator rejecting an ambient payload, and the task-mode
`unrelated-frame` assertion re-run unchanged.
`tests/test_habit_bridge.py` covers absent rows as coverage rather than events,
civil-time induction, the no-coverage safety property, task rows never being
read as ambient, and the cross-midnight regression.
`tests/test_ambient_console.py` covers off-and-undisclosed by default, consent
required and revocable, no bedroom in the shipped watchlist, and consent
appearing in export and being removed with the account.
