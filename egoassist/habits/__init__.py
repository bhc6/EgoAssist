"""Habit induction from passive observation, and the reminder gate it feeds.

:mod:`.induction` holds the pure statistics (circular time-of-day fitting,
modal location, the three support gates). :mod:`.bridge` connects them to
stored ambient observations and decides whether to remind.
"""

from .induction import (
    MINUTES_PER_DAY,
    Event,
    Habit,
    circular_time_stats,
    induce_habits,
    minutes_of_day,
    should_remind,
)
from .bridge import (
    AMBIENT_TASK_PREFIX,
    ambient_reminder_decision,
    ambient_task_id,
    habit_profiles,
    observation_coverage,
    observation_events,
)

__all__ = [
    "MINUTES_PER_DAY",
    "Event",
    "Habit",
    "circular_time_stats",
    "induce_habits",
    "minutes_of_day",
    "should_remind",
    "AMBIENT_TASK_PREFIX",
    "ambient_reminder_decision",
    "ambient_task_id",
    "habit_profiles",
    "observation_coverage",
    "observation_events",
]
