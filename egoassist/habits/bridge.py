"""Bridge stored ambient observations into habit induction and reminder gating.

This is the layer that makes passive observation useful: ambient rows recorded
by ``validate_ambient_result`` are read back, projected into the participant's
civil time, and fed to the pure statistics in :mod:`.induction`.

The load-bearing distinction in this module is between *not observed* and *not
watched*:

* An ambient row with ``checkpoint_id == "none-observed"`` means the camera
  looked and the behaviour was not happening. It is not a habit event, but it
  *is* proof that the day was covered.
* No row at all means nothing was watched -- the glasses were off, the battery
  died, consent was revoked, the participant went out.

Conflating the two turns a flat battery into a missed-dose alert, which is
exactly the false alarm the design brief (§6.4) forbids. So a reminder is only
ever emitted for a local day that has observation coverage.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..memory.temporal import ClockContext
from ..realtime.ambient import NONE_OBSERVED, UNKNOWN_LOCATION
from .induction import Event, Habit, induce_habits, minutes_of_day

AMBIENT_TASK_PREFIX = "ambient:"


def ambient_task_id(watchlist_id: str) -> str:
    """The reserved task id ambient rows are stored under."""
    return f"{AMBIENT_TASK_PREFIX}{watchlist_id}"


def _row_local_time(row: Mapping[str, Any], clock: ClockContext) -> datetime | None:
    raw = row.get("frame_captured_at")
    if not raw:
        return None
    if isinstance(raw, datetime):
        moment = raw
    else:
        try:
            moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    # Civil time, not UTC: an 08:00 Beijing habit induced on UTC timestamps
    # would be learnt as 00:00 and the reminder would fire in the night.
    return moment.astimezone(clock.tzinfo)


def _evidence(row: Mapping[str, Any]) -> Mapping[str, Any]:
    value = row.get("evidence")
    return value if isinstance(value, Mapping) else {}


def _is_ambient(row: Mapping[str, Any]) -> bool:
    return str(row.get("task_id", "")).startswith(AMBIENT_TASK_PREFIX)


def observation_events(
    rows: Iterable[Mapping[str, Any]],
    *,
    clock: ClockContext,
    activity_labels: Mapping[str, str] | None = None,
    location_labels: Mapping[str, str] | None = None,
) -> list[Event]:
    """Project stored ambient rows into habit-induction events.

    Only positive sightings become events. ``none-observed`` rows are dropped
    here and counted by :func:`observation_coverage` instead.

    Labels are looked up from the watchlist when supplied so induced habits read
    in the participant's own words; ids are used verbatim otherwise. No free-text
    parsing of summaries -- the structured fields are the only input.
    """
    activity_labels = activity_labels or {}
    location_labels = location_labels or {}
    events: list[Event] = []
    for row in rows:
        if not _is_ambient(row):
            continue
        evidence = _evidence(row)
        activity_id = str(evidence.get("activityId") or row.get("checkpoint_id") or "")
        if not activity_id or activity_id == NONE_OBSERVED:
            continue
        if evidence.get("observed") is False:
            continue
        moment = _row_local_time(row, clock)
        if moment is None:
            continue
        location_id = str(evidence.get("locationId") or UNKNOWN_LOCATION)
        events.append(
            Event(
                timestamp=moment,
                location=location_labels.get(location_id, location_id),
                activity=activity_labels.get(activity_id, activity_id),
            )
        )
    return events


def observation_coverage(
    rows: Iterable[Mapping[str, Any]],
    *,
    clock: ClockContext,
) -> dict[date, dict[str, Any]]:
    """Per local day: how much the camera actually watched.

    Every ambient row counts towards coverage, including ``none-observed`` ones
    -- that is the point of the function. ``first``/``last`` bound the watched
    interval so a caller can tell a day watched only at breakfast from one
    watched throughout.
    """
    coverage: dict[date, dict[str, Any]] = {}
    for row in rows:
        if not _is_ambient(row):
            continue
        moment = _row_local_time(row, clock)
        if moment is None:
            continue
        day = moment.date()
        bucket = coverage.setdefault(
            day,
            {"frames": 0, "observed": 0, "absent": 0, "first": moment, "last": moment},
        )
        bucket["frames"] += 1
        evidence = _evidence(row)
        activity_id = str(evidence.get("activityId") or row.get("checkpoint_id") or "")
        if evidence.get("observed") is True and activity_id != NONE_OBSERVED:
            bucket["observed"] += 1
        else:
            bucket["absent"] += 1
        if moment < bucket["first"]:
            bucket["first"] = moment
        if moment > bucket["last"]:
            bucket["last"] = moment
    return coverage


def habit_profiles(
    rows: Iterable[Mapping[str, Any]],
    *,
    clock: ClockContext,
    activity_labels: Mapping[str, str] | None = None,
    location_labels: Mapping[str, str] | None = None,
    freq_threshold: float = 0.5,
    std_threshold_min: float = 90.0,
    min_observations: int = 5,
) -> list[Habit]:
    """Induce habits from stored ambient rows. Returns [] when support is thin."""
    events = observation_events(
        rows,
        clock=clock,
        activity_labels=activity_labels,
        location_labels=location_labels,
    )
    return induce_habits(
        events,
        freq_threshold=freq_threshold,
        std_threshold_min=std_threshold_min,
        min_observations=min_observations,
    )


def _observed_today(
    rows: Iterable[Mapping[str, Any]],
    *,
    clock: ClockContext,
    activity: str,
    activity_labels: Mapping[str, str] | None = None,
) -> bool:
    labels = activity_labels or {}
    today = clock.local_now.date()
    for row in rows:
        if not _is_ambient(row):
            continue
        evidence = _evidence(row)
        activity_id = str(evidence.get("activityId") or row.get("checkpoint_id") or "")
        if not activity_id or activity_id == NONE_OBSERVED:
            continue
        if evidence.get("observed") is False:
            continue
        if labels.get(activity_id, activity_id) != activity:
            continue
        moment = _row_local_time(row, clock)
        if moment is not None and moment.date() == today:
            return True
    return False


def ambient_reminder_decision(
    rows: Sequence[Mapping[str, Any]],
    *,
    clock: ClockContext,
    activity_labels: Mapping[str, str] | None = None,
    location_labels: Mapping[str, str] | None = None,
    grace_min: float = 30.0,
    window_sigma: float = 1.0,
    lookback_min: float = 720.0,
    min_coverage_frames: int = 1,
    freq_threshold: float = 0.5,
    std_threshold_min: float = 90.0,
    min_observations: int = 5,
) -> dict[str, Any]:
    """Decide whether to remind, with the reason always stated explicitly.

    The "do nothing" branch is a first-class outcome, not a fallthrough. Every
    return carries a ``reason`` so a caller -- or a reviewer reading the ledger
    months later -- can say why the system spoke or stayed quiet.

    Returns ``{"remind": bool, "reason": str, ...}``. The caller is responsible
    for delivery, and must route it through the reminder workflow rather than
    speaking directly, so the decision stays auditable and cancellable.
    """
    rows = list(rows)
    habits = habit_profiles(
        rows,
        clock=clock,
        activity_labels=activity_labels,
        location_labels=location_labels,
        freq_threshold=freq_threshold,
        std_threshold_min=std_threshold_min,
        min_observations=min_observations,
    )
    if not habits:
        return {
            "remind": False,
            "reason": "no-induced-habit",
            "habit": None,
            "coverageFrames": 0,
        }

    coverage = observation_coverage(rows, clock=clock)
    today = clock.local_now.date()
    today_coverage = coverage.get(today, {"frames": 0})
    frames_today = int(today_coverage.get("frames", 0))
    if frames_today < max(1, int(min_coverage_frames)):
        # Nothing watched today. Absence of a sighting is not evidence of a
        # missed dose, so stay quiet rather than guess.
        return {
            "remind": False,
            "reason": "no-observation-coverage-today",
            "habit": None,
            "coverageFrames": frames_today,
        }

    now = clock.local_now
    now_min = minutes_of_day(now)
    for habit in habits:
        seen = _observed_today(
            rows, clock=clock, activity=habit.activity, activity_labels=activity_labels
        )
        window_end = habit.mean_time_min + window_sigma * habit.std_time_min + grace_min
        elapsed = (now_min - window_end) % (24 * 60)
        payload = {
            "activity": habit.activity,
            "expectedTime": habit.time_hhmm(),
            "expectedLocation": habit.mode_location,
            "stdMinutes": round(habit.std_time_min, 1),
            "frequency": round(habit.frequency, 3),
            "observations": habit.n_observations,
        }
        if seen:
            return {
                "remind": False,
                "reason": "already-observed-today",
                "habit": payload,
                "coverageFrames": frames_today,
            }
        if elapsed >= lookback_min:
            continue
        if elapsed <= 0:
            continue
        return {
            "remind": True,
            "reason": "expected-window-elapsed-without-observation",
            "habit": payload,
            "coverageFrames": frames_today,
            "minutesLate": round(elapsed, 1),
        }

    return {
        "remind": False,
        "reason": "inside-expected-window",
        "habit": None,
        "coverageFrames": frames_today,
    }
