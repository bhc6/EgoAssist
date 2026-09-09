"""Bounded report execution, persisted in the existing user session journal."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import time
from uuid import uuid4

from egoassist.memory.insights import calendar_period
from egoassist.memory.temporal import ClockContext

PENDING = {"queued", "running"}


def insight_jobs(journal) -> list[dict]:
    latest = {}
    for row in journal.entries():
        if row.get("type") == "insight.job":
            latest[row["jobId"]] = row
    return list(latest.values())


class InsightQueueFull(ValueError):
    pass


class InsightJobs:
    def __init__(self, generator, resolve, *, concurrency=1, capacity=16):
        self.generator = generator
        self.resolve = resolve
        self.capacity = capacity
        if concurrency < 1 or capacity < concurrency:
            raise ValueError("report capacity must cover positive concurrency")
        self._slots = asyncio.Semaphore(concurrency)
        self._tasks = {}
        self._users = {}
        self._closing = False
        self._blocked_users = set()
        self.timeout = float(os.getenv("EGOASSIST_INSIGHT_JOB_TIMEOUT", "45"))
        if not 1 <= self.timeout <= 300:
            raise ValueError("invalid report job timeout")

    def submit(self, *, user_id, session_id, journal, kind, scope, anchor, clock):
        if self._closing or user_id in self._blocked_users:
            raise ValueError("report execution is stopping")
        period = calendar_period(kind, anchor, clock)
        if scope not in {"case", "account"}:
            raise ValueError("invalid report scope")
        specification = {"kind": kind, "scope": scope, "anchor": period["anchor"],
                         "timezone": clock.timezone_name, "utcOffsetMinutes": clock.utc_offset_minutes}
        previous = next((row for row in reversed(insight_jobs(journal))
                         if row["status"] in PENDING and row["specification"] == specification), None)
        if previous:
            return {**previous, "deduplicated": True}
        if len(self._tasks) >= self.capacity:
            raise InsightQueueFull("report queue is full")
        row = journal.append("insight.job", {"jobId": uuid4().hex[:16], "status": "queued",
            "specification": specification, "attempt": 0, "createdAt": datetime.now(timezone.utc).isoformat()})
        self._schedule(user_id, session_id, journal, row)
        return row

    def _schedule(self, user_id, session_id, journal, row):
        job_id = row["jobId"]
        self._users[job_id] = user_id
        task = asyncio.create_task(self._execute(user_id, session_id, journal, row), name="insight-" + job_id)
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: (self._tasks.pop(job_id, None), self._users.pop(job_id, None)))

    @staticmethod
    def _transition(journal, previous, **updates):
        fields = {key: value for key, value in previous.items() if key not in {"id", "type", "timestamp", "parentId"}}
        return journal.append("insight.job", {**fields, **updates}, parent_id=previous["id"])

    async def _execute(self, user_id, session_id, journal, row):
        try:
            async with self._slots:
                row = self._transition(journal, row, status="running", attempt=row["attempt"] + 1,
                    startedAt=datetime.now(timezone.utc).isoformat())
                started = time.perf_counter()
                spec = row["specification"]
                paths = self.resolve(user_id, session_id, spec["scope"])
                clock = ClockContext.current(spec["timezone"], spec["utcOffsetMinutes"])
                result = await asyncio.wait_for(self.generator.generate(user_id=user_id, journal=journal,
                    paths=paths, kind=spec["kind"], anchor=spec["anchor"], clock=clock), timeout=self.timeout)
                self._transition(journal, row, status=result["status"], resultEventId=result.get("id"),
                    error=result.get("error"), durationMs=round((time.perf_counter() - started) * 1000, 3),
                    finishedAt=datetime.now(timezone.utc).isoformat())
        except asyncio.CancelledError:
            self._transition(journal, row, status="queued" if self._closing else "cancelled",
                             reason="server-shutdown" if self._closing else "cancel-requested")
            raise
        except Exception as exc:
            self._transition(journal, row, status="failed", error=type(exc).__name__ + ": " + str(exc)[:200],
                             timedOut=isinstance(exc, asyncio.TimeoutError),
                             finishedAt=datetime.now(timezone.utc).isoformat())

    def recover(self, user_id, session_id, journal):
        if self._closing or user_id in self._blocked_users:
            return
        for row in insight_jobs(journal):
            if row["status"] not in PENDING or row["jobId"] in self._tasks:
                continue
            if row.get("attempt", 0) >= 2:
                self._transition(journal, row, status="failed", reason="restart-attempt-limit")
            elif len(self._tasks) >= self.capacity:
                # Keep it persisted; a later case refresh can resume it.
                continue
            else:
                self._schedule(user_id, session_id, journal,
                               self._transition(journal, row, status="queued", reason="recovered"))

    async def cancel(self, job_id, journal):
        row = next((item for item in insight_jobs(journal) if item["jobId"] == job_id), None)
        if row is None:
            raise ValueError("unknown report job")
        task = self._tasks.get(job_id)
        if row["status"] in PENDING:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            latest = next(item for item in insight_jobs(journal) if item["jobId"] == job_id)
            if latest["status"] in PENDING:
                self._transition(journal, latest, status="cancelled", reason="cancel-requested")

    async def delete_user(self, user_id):
        self._blocked_users.add(user_id)
        tasks = [task for key, task in tuple(self._tasks.items()) if self._users[key] == user_id]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        self._closing = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
