from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from egoassist import CONSOLE_ROOT
from egoassist.realtime import (
    CaseWorkLedger,
    ContextHarness,
    SessionJournal,
    SkillRuntime,
    SkillToolExecutor,
)

WEB_ROOT = CONSOLE_ROOT

LOGGER = logging.getLogger("egoassist.realtime")

AUTH_COOKIE = "egoassist_session"

TASK_CONTROL_LABELS = {
    None,
    "confirm-step",
    "rollback",
    "repeat",
    "pause-task",
    "resume-task",
    "request-help",
    "show-reference",
}

SKILL_CONTROL_LABELS = {None, "deactivate"}

AMBIENT_CONSENT_PURPOSE = "ambient-observation"


class RuntimeRootLease:
    """Process-lifetime exclusive lease for one durable runtime root."""

    def __init__(self, memory_path: str | Path):
        self.path = Path(memory_path).resolve().parent / ".egoassist-server.lock"
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise RuntimeError(
                f"EgoAssist runtime root is already owned by another process: {self.path.parent}. "
                "Use a different --memory-path for a second server."
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class CaseOwnerCapacityError(RuntimeError):
    """Raised when every bounded case owner is still active."""


class MemoryEventBus:
    """Per-session event bus for memory operation observability.

    Phase 1 research instrumentation: captures encode/retrieve/compact/working_memory
    operations as structured JSONL records and forwards them to the WebSocket frontend
    for real-time monitoring.

    Designed for memory mechanism research — provides clean hooks for analyzing
    memory layer behavior without modifying core business logic.
    """

    _MAX_RECENT = 200

    def __init__(self, session_id: str, trace_path: Path):
        self.session_id = session_id
        self._trace_path = trace_path
        self._recent: deque = deque(maxlen=self._MAX_RECENT)
        self._lock = threading.Lock()
        self._ws_emitter: Callable | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_ws_emitter(
        self, emitter: Callable, loop: asyncio.AbstractEventLoop
    ) -> None:
        """Update the WebSocket send function and event loop.

        Called on each new WebSocket connection. The emitter is used to forward
        memory events to the frontend console in real time.
        """
        with self._lock:
            self._ws_emitter = emitter
            self._loop = loop

    def record(self, op: str, **payload) -> dict:
        """Record a memory operation event synchronously.

        Writes to:
        1. In-memory ring buffer (for /api/memory/state queries)
        2. memory_trace.jsonl (persistent research log)
        3. WebSocket frontend (if connected, non-blocking)

        Returns the recorded event dict.
        """
        event = {
            "op": op,
            "session_id": self.session_id,
            "ts": time.time(),
            **payload,
        }

        # 1. Ring buffer for /api/memory/state
        with self._lock:
            self._recent.append(event)
            emitter = self._ws_emitter
            loop = self._loop

        # 2. JSONL file
        try:
            with open(self._trace_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass  # Non-fatal: trace file write failure doesn't block operation

        # 3. WebSocket (non-blocking via run_coroutine_threadsafe)
        if emitter is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    emitter({"type": "memory_trace", **event}),
                    loop,
                )
            except Exception:
                pass  # WebSocket send failure is non-fatal

        return event

    def emit_event(self, event_type: str, **payload) -> None:
        """Emit a typed WebSocket event without writing to the memory trace log.

        Used for transient pipeline events (task lifecycle, routing decisions) that
        are not memory-layer operations and should not appear in the JSONL trace.
        Thread-safe; safe to call from DashScope SDK threads.
        """
        with self._lock:
            emitter = self._ws_emitter
            loop = self._loop
        if emitter is not None and loop is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    emitter(
                        {
                            "type": event_type,
                            "session_id": self.session_id,
                            "ts": time.time(),
                            **payload,
                        }
                    ),
                    loop,
                )
            except Exception:
                pass  # WS send failure is non-fatal

    def recent(self, n: int = 40) -> list[dict]:
        """Return the n most recent operations (newest last)."""
        with self._lock:
            items = list(self._recent)
        return items[-n:] if items else []


@dataclass
class TaskSlot:
    """Lightweight execution record for one background intent dispatched in a user turn.

    Tracks lifecycle (pending → running → done / failed / cancelled) and exposes
    a short text preview for the console task-queue panel.  Slots are per-session
    and are NOT persisted across server restarts.
    """

    task_id: str
    kind: str  # background-job / persistent-task / …
    text: str  # intent text from the compound turn
    status: str  # pending / running / done / failed / cancelled
    created_at: float
    turn_id: str | None = None
    intent_id: str | None = None
    job_id: str | None = None
    delivery: str = "display"
    completed_at: float | None = None
    result_preview: str | None = None
    asyncio_task: Any = field(default=None, repr=False, compare=False)


@dataclass
class CaseRuntimeOwner:
    """Single in-process owner for one durable experiment case."""

    user_id: str
    session_id: str
    journal: SessionJournal
    harness: ContextHarness
    runtime: SkillRuntime
    routing: dict
    tools: SkillToolExecutor
    mutation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    epoch: int = 0
    socket_token: object | None = None
    socket: Any = None
    turn_queue: asyncio.Queue | None = None
    turn_worker: asyncio.Task | None = None
    maintenance_task: asyncio.Task | None = None
    consolidation_requested: bool = False
    transitioning: bool = False
    turn_sequence: int = 0
    latest_input_sequence: int = 0
    # Input receipt never owns speech. Only a validated semantic decision can
    # supersede this floor; backchannels and bystanders leave it untouched.
    speech_floor_sequence: int = 0
    pending_semantic_sequences: set[int] = field(default_factory=set)
    # Monotonic fence for user-visible foreground output. Background work may
    # outlive this value, but foreground responses and tools may not.
    output_generation: int = 0
    work_ledger: CaseWorkLedger | None = None
    classification_tasks: set[asyncio.Task] = field(default_factory=set)
    prepared_scenes: dict[str, asyncio.Task] = field(default_factory=dict)
    applied_controls: dict[str, dict] = field(default_factory=dict)
    queued_turn_ids: set[str] = field(default_factory=set)
    job_scheduler_task: asyncio.Task | None = None
    bridge: Any = None
    tool_confirmation_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    ambient_options: dict | None = None
    intent_runtimes: dict[str, SkillRuntime] = field(default_factory=dict)
    memory_bus: Any = None  # MemoryEventBus — Phase 1 observability
    background_tasks: dict[str, "TaskSlot"] = field(
        default_factory=dict
    )  # Phase 2A: fanout slots
    # AmbientWatchSession — present only while ambient mode is on AND consent is
    # live. Cleared on revocation, so it is never a stale authorization.
    ambient: Any = None
    interaction: Any = None
    scene: Any = None
    lanes: Any = field(default=None, init=False)

    def __post_init__(self):
        from egoassist.runtime import ThreeLaneRuntime

        self.lanes = ThreeLaneRuntime(self.journal, self.user_id)
