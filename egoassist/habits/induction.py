"""Habit induction from symbolic observation logs.

Given events of the form (timestamp, location, activity), fit each recurring
activity's time-of-day with a circular (von Mises) distribution so midnight
wrap is handled correctly, take the modal location, and emit a Habit only when
frequency, regularity, and sample count all pass.

The statistics live here rather than in the realtime layer because they are
pure and independently testable. See habit_induction.py at the repository root
for the standalone reference/demo version of the same maths.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

import numpy as np

try:
    from scipy.stats import vonmises

    _HAS_SCIPY = True
except ImportError:  # scipy is optional; fall back to circular moment estimates
    _HAS_SCIPY = False


MINUTES_PER_DAY = 24 * 60


@dataclass
class Event:
    """One symbolic observation: when, where, what."""

    timestamp: datetime
    location: str
    activity: str


@dataclass
class Habit:
    """An induced habit profile."""

    activity: str
    mean_time_min: float          # minutes since local midnight
    std_time_min: float           # circular standard deviation in minutes
    mode_location: str
    location_prob: float          # [0,1]
    frequency: float              # active days / observed days, [0,1]
    consistency: float            # 1 / (std + eps), for ranking and gating
    n_observations: int

    def time_hhmm(self) -> str:
        h, m = divmod(int(round(self.mean_time_min)) % MINUTES_PER_DAY, 60)
        return f"{h:02d}:{m:02d}"

    def summary(self) -> str:
        return (
            f"{self.activity} ~{self.time_hhmm()}±{int(round(self.std_time_min))}min, "
            f"{self.mode_location}({self.location_prob:.0%}), "
            f"freq {self.frequency:.2f}, n={self.n_observations}"
        )


def minutes_of_day(ts: datetime) -> float:
    return ts.hour * 60 + ts.minute + ts.second / 60.0


def _to_angle(minutes: np.ndarray) -> np.ndarray:
    return minutes / MINUTES_PER_DAY * 2 * math.pi


def _from_angle(angle: float) -> float:
    return (angle % (2 * math.pi)) / (2 * math.pi) * MINUTES_PER_DAY


def circular_time_stats(minutes: np.ndarray) -> tuple[float, float]:
    """Return (circular mean minutes, circular standard deviation minutes).

    std = sqrt(-2 * ln(R)) in radians, where R is the resultant vector length
    in [0,1]; R -> 1 means highly concentrated (std -> 0).
    """
    angles = _to_angle(minutes)

    if _HAS_SCIPY and len(minutes) >= 2:
        _kappa, loc, _ = vonmises.fit(angles, fscale=1)
        mean_min = _from_angle(loc)
    else:
        c = float(np.mean(np.cos(angles)))
        s = float(np.mean(np.sin(angles)))
        mean_min = _from_angle(math.atan2(s, c))

    R = float(np.hypot(float(np.mean(np.cos(angles))), float(np.mean(np.sin(angles)))))
    std_rad = math.sqrt(-2.0 * math.log(R)) if 0 < R < 1 else 0.0
    std_min = std_rad / (2 * math.pi) * MINUTES_PER_DAY
    return mean_min, std_min


def induce_habits(
    events: list[Event],
    freq_threshold: float = 0.5,
    std_threshold_min: float = 90.0,
    min_observations: int = 5,
) -> list[Habit]:
    """Induce habits, sorted by consistency descending.

    All three gates must pass: enough observations, frequent enough, and
    regular enough. Biased towards emitting nothing rather than emitting a
    weakly-supported habit that could drive an unwanted reminder.
    """
    if not events:
        return []

    all_days = {e.timestamp.date() for e in events}
    total_days = max(len(all_days), 1)

    by_activity: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_activity[e.activity].append(e)

    habits: list[Habit] = []
    eps = 1e-6

    for activity, evs in by_activity.items():
        n = len(evs)
        if n < min_observations:
            continue

        minutes = np.array([minutes_of_day(e.timestamp) for e in evs], dtype=float)
        mean_min, std_min = circular_time_stats(minutes)

        loc_counter = Counter(e.location for e in evs)
        mode_location, mode_count = loc_counter.most_common(1)[0]
        location_prob = mode_count / n

        active_days = {e.timestamp.date() for e in evs}
        frequency = len(active_days) / total_days

        if frequency < freq_threshold:
            continue
        if std_min > std_threshold_min:
            continue

        habits.append(
            Habit(
                activity=activity,
                mean_time_min=mean_min,
                std_time_min=std_min,
                mode_location=mode_location,
                location_prob=location_prob,
                frequency=frequency,
                consistency=1.0 / (std_min + eps),
                n_observations=n,
            )
        )

    habits.sort(key=lambda h: h.consistency, reverse=True)
    return habits


def should_remind(
    habit: Habit,
    now: datetime,
    observed_today: bool,
    grace_min: float = 30.0,
    window_sigma: float = 1.0,
    lookback_min: float = 720.0,
) -> bool:
    """Observation-gated reminder rule. No ML, just negative-event timing.

    Remind only once the expected window has fully elapsed -- past
    ``mean + window_sigma*std + grace`` -- and the behaviour still has not been
    observed. Staying quiet inside the window biases towards false negatives
    over false alarms: a missed reminder is recoverable, a wrong one erodes
    trust and gets the whole system switched off.

    Any observation cancels the reminder immediately.

    The comparison is circular, so a habit whose window ends after midnight
    still fires. ``lookback_min`` bounds how long after the window a reminder
    remains useful; past that the moment has gone and nagging does not help.
    """
    if observed_today:
        return False

    now_min = minutes_of_day(now)
    window_end = habit.mean_time_min + window_sigma * habit.std_time_min + grace_min
    elapsed = (now_min - window_end) % MINUTES_PER_DAY
    return elapsed < lookback_min
