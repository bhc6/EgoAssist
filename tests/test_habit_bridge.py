"""Habit bridge tests.

The distinction this module exists to protect: an ambient row saying "looked,
saw nothing" is coverage; no row at all is a blind spot. A reminder may only
follow the first. Conflating them turns a flat battery into a missed-dose alert.
"""

from datetime import datetime, timedelta, timezone

from egoassist.habits import (
    Event,
    Habit,
    ambient_reminder_decision,
    ambient_task_id,
    habit_profiles,
    induce_habits,
    observation_coverage,
    observation_events,
    should_remind,
)
from egoassist.memory.temporal import ClockContext


BEIJING = timezone(timedelta(hours=8))
TASK_ID = ambient_task_id("medication-adherence")


def clock_at(local: datetime) -> ClockContext:
    """A ClockContext whose civil now is the given naive Beijing wall-clock time."""
    return ClockContext.current(
        "Asia/Shanghai", 480, now_utc=local.replace(tzinfo=BEIJING).astimezone(timezone.utc)
    )


def row(local: datetime, *, observed: bool, location: str = "kitchen", task_id: str = TASK_ID) -> dict:
    activity = "take-medication" if observed else "none-observed"
    return {
        "task_id": task_id,
        "checkpoint_id": activity,
        "frame_captured_at": local.replace(tzinfo=BEIJING).astimezone(timezone.utc).isoformat(),
        "evidence": {
            "observed": observed,
            "activityId": activity,
            "locationId": location if observed else "unknown",
        },
    }


def daily_rows(days: int = 12, *, hour: int = 8, minute: int = 0, start_day: int = 20) -> list[dict]:
    """One sighting plus one coverage-only frame per day through late August."""
    values: list[dict] = []
    for offset in range(days):
        day = datetime(2026, 8, start_day + offset, hour, minute)
        values.append(row(day + timedelta(minutes=(offset % 5) - 2), observed=True))
        values.append(row(datetime(2026, 8, start_day + offset, 12, 0), observed=False))
    return values


def test_absent_rows_are_coverage_not_events():
    rows = daily_rows()
    clock = clock_at(datetime(2026, 9, 1, 9, 0))
    events = observation_events(rows, clock=clock)
    coverage = observation_coverage(rows, clock=clock)
    assert len(events) == 12, "only sightings become habit events"
    assert len(coverage) == 12
    assert all(bucket["frames"] == 2 and bucket["absent"] == 1 for bucket in coverage.values())


def test_habits_are_induced_in_civil_time_not_utc():
    """An 08:00 Beijing habit induced on UTC timestamps would be learnt as 00:00
    and the reminder would fire in the middle of the night."""
    habits = habit_profiles(daily_rows(), clock=clock_at(datetime(2026, 9, 1, 9, 0)))
    assert len(habits) == 1
    assert habits[0].time_hhmm().startswith("07:5") or habits[0].time_hhmm().startswith("08:0")
    assert habits[0].mode_location == "kitchen"
    assert habits[0].n_observations == 12


def test_reminder_fires_once_the_window_elapsed_without_a_sighting():
    rows = daily_rows() + [row(datetime(2026, 9, 1, 7, 0), observed=False)]
    decision = ambient_reminder_decision(rows, clock=clock_at(datetime(2026, 9, 1, 10, 0)))
    assert decision["remind"] is True
    assert decision["reason"] == "expected-window-elapsed-without-observation"
    assert decision["coverageFrames"] == 1


def test_a_sighting_today_cancels_the_reminder():
    rows = daily_rows() + [
        row(datetime(2026, 9, 1, 7, 0), observed=False),
        row(datetime(2026, 9, 1, 8, 5), observed=True),
    ]
    decision = ambient_reminder_decision(rows, clock=clock_at(datetime(2026, 9, 1, 10, 0)))
    assert decision["remind"] is False
    assert decision["reason"] == "already-observed-today"


def test_no_coverage_today_suppresses_the_reminder():
    """The safety property. With no frames today the system cannot distinguish a
    missed dose from glasses left on the charger, so it must stay quiet."""
    decision = ambient_reminder_decision(
        daily_rows(), clock=clock_at(datetime(2026, 9, 3, 10, 0))
    )
    assert decision["remind"] is False
    assert decision["reason"] == "no-observation-coverage-today"
    assert decision["coverageFrames"] == 0


def test_inside_the_window_stays_quiet():
    rows = daily_rows() + [row(datetime(2026, 9, 1, 7, 0), observed=False)]
    decision = ambient_reminder_decision(rows, clock=clock_at(datetime(2026, 9, 1, 7, 30)))
    assert decision["remind"] is False
    assert decision["reason"] == "inside-expected-window"


def test_thin_support_induces_no_habit_and_no_reminder():
    rows = [row(datetime(2026, 8, 20 + offset, 8, 0), observed=True) for offset in range(3)]
    assert habit_profiles(rows, clock=clock_at(datetime(2026, 9, 1, 10, 0))) == []
    decision = ambient_reminder_decision(rows, clock=clock_at(datetime(2026, 9, 1, 10, 0)))
    assert decision["remind"] is False
    assert decision["reason"] == "no-induced-habit"


def test_task_rows_are_never_read_as_ambient_observations():
    rows = daily_rows() + [row(datetime(2026, 9, 1, 8, 0), observed=True, task_id="assemble-shelf")]
    clock = clock_at(datetime(2026, 9, 1, 10, 0))
    assert len(observation_events(rows, clock=clock)) == 12
    assert observation_coverage(rows, clock=clock).get(datetime(2026, 9, 1).date()) is None


def test_labels_render_habits_in_the_participants_words():
    habits = habit_profiles(
        daily_rows(),
        clock=clock_at(datetime(2026, 9, 1, 9, 0)),
        activity_labels={"take-medication": "服药"},
        location_labels={"kitchen": "厨房"},
    )
    assert habits[0].activity == "服药"
    assert habits[0].mode_location == "厨房"


def test_cross_midnight_habit_still_reminds_after_the_window():
    """Regression: a window ending after 24:00 exceeds 1440 minutes, so a plain
    ``now >= window_end`` comparison never fires. The gate is circular."""
    late = Habit(
        activity="take-medication",
        mean_time_min=23 * 60 + 50,
        std_time_min=5.0,
        mode_location="kitchen",
        location_prob=1.0,
        frequency=1.0,
        consistency=1.0,
        n_observations=10,
    )
    assert should_remind(late, datetime(2026, 9, 8, 0, 30), False) is True
    assert should_remind(late, datetime(2026, 9, 7, 23, 55), False) is False
    assert should_remind(late, datetime(2026, 9, 8, 0, 30), True) is False
    # Half a day late the moment has passed; nagging does not help.
    assert should_remind(late, datetime(2026, 9, 8, 14, 0), False) is False


def test_midnight_spanning_events_average_correctly():
    """Circular statistics: 23:50 and 00:10 average to midnight, not to noon."""
    events = [
        Event(datetime(2026, 8, 20 + offset, 23, 50), "kitchen", "take-medication")
        for offset in range(5)
    ] + [
        Event(datetime(2026, 8, 26 + offset, 0, 10), "kitchen", "take-medication")
        for offset in range(5)
    ]
    habit = induce_habits(events, min_observations=5)[0]
    assert habit.time_hhmm() in {"23:59", "00:00", "00:01"}
