"""Small process-local metrics for memory reads and writes.

These measurements describe stage behavior only. They are deliberately kept
separate from model quality metrics and never include user text or evidence.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
import sqlite3
import threading
import time
import math

_active = ContextVar('memory_stage_metrics', default=None)


@contextmanager
def measure_storage(metrics):
    token = _active.set(metrics)
    try:
        yield
    finally:
        _active.reset(token)


class MeasuredConnection(sqlite3.Connection):
    """Measure acquisition of SQLite's writer lock, without altering timeouts."""
    def execute(self, sql, parameters=()):
        metrics = _active.get()
        lock_acquisition = str(sql).strip().upper() == 'BEGIN IMMEDIATE'
        started = time.perf_counter()
        try:
            return super().execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            if metrics and (getattr(exc, 'sqlite_errorcode', 0) & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                metrics.increment('sqlite.busyErrors')
            raise
        finally:
            if metrics and lock_acquisition:
                metrics.timing('sqlite.writerAcquire', (time.perf_counter() - started) * 1000)


class StageMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = defaultdict(int)
        self._timings = defaultdict(lambda: {"count": 0, "totalMs": 0.0, "maxMs": 0.0})
        self._recent = defaultdict(lambda: deque(maxlen=256))

    def increment(self, name: str, value: int = 1):
        with self._lock:
            self._counts[str(name)] += int(value)

    def timing(self, name: str, milliseconds: float):
        value = max(0.0, float(milliseconds))
        with self._lock:
            item = self._timings[str(name)]
            item["count"] += 1
            item["totalMs"] += value
            item["maxMs"] = max(item["maxMs"], value)
            self._recent[str(name)].append(value)

    def snapshot(self):
        with self._lock:
            timings = {
                name: {
                    **item,
                    "totalMs": round(item["totalMs"], 3),
                    "maxMs": round(item["maxMs"], 3),
                    "averageMs": round(item["totalMs"] / item["count"], 3)
                    if item["count"] else 0.0,
                    'p95RecentMs': round(sorted(self._recent[name])[math.ceil(len(self._recent[name]) * .95) - 1], 3),
                    'recentSamples': len(self._recent[name]),
                }
                for name, item in self._timings.items()
            }
            counts = dict(self._counts)
            def rate(numerator, denominator):
                return round(counts.get(numerator, 0) / counts[denominator], 6) if counts.get(denominator) else None
            return {"counts": counts, "timings": timings,
                    'rates': {'timeoutPerPrepare': rate('prepare.timeout', 'prepare.requests'),
                              'degradedPerPrepare': rate('prepare.degraded', 'prepare.requests'),
                              'rejectedPerAccept': rate('accept.rejected', 'accept.requests'),
                              'failurePerCommit': rate('commit.failed', 'commit.requests')},
                    "claimBoundary": "Process-local memory stage measurements; no model quality or end-to-end latency claim."}


class MemoryMetrics:
    """Bounded, owner-scoped counters; never expose another account's activity."""
    def __init__(self, capacity=512):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._scopes = OrderedDict()

    def for_scope(self, user_id, session_id=None):
        key = (user_id, session_id)
        with self._lock:
            if key not in self._scopes:
                self._scopes[key] = StageMetrics()
            self._scopes.move_to_end(key)
            while len(self._scopes) > self.capacity:
                self._scopes.popitem(last=False)
            return self._scopes[key]

    def snapshot(self, user_id, session_id=None):
        return {**self.for_scope(user_id, session_id).snapshot(), 'schemaVersion': 'memory-metrics.v1',
                'scope': 'case' if session_id else 'account-sessionless',
                'sessionId': session_id, 'retention': 'process-lifetime-or-LRU-eviction'}
