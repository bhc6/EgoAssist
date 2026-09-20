"""Durable, typed context harness for realtime sessions.

The local journal is the source of truth. Provider sessions are disposable
projections that can be rebuilt from the latest compaction checkpoint.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

JOURNAL_VERSION = 1
_VALID_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MESSAGE_ROLES = {"user", "assistant"}


class EvidenceUnavailable(ValueError):
    """A dependency was erased before a derived write could commit."""


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
# JSONL is the source of truth; the SQLite projection does not protect its tail.
# Default appends flush to the OS. Set EGOASSIST_JOURNAL_FSYNC=1 for per-entry
# disk synchronization; without it, power loss can lose acknowledged tail data.
_JOURNAL_FSYNC: bool = os.getenv("EGOASSIST_JOURNAL_FSYNC", "0").strip() not in {
    "",
    "0",
    "false",
    "no",
}


def _path_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _file_lock(path: Path, *, create=True):
    """Serialize journal transactions across processes on Windows and POSIX."""
    lock_path = path.with_name(path.name + ".lock")
    if create:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b" if create else "r+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SessionJournal:
    """Append-only JSONL session storage with idempotent entry keys."""

    def __init__(
        self,
        path: str | Path,
        *,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self._lock = _path_lock(self.path)
        self.recovered_partial_tail = False
        self._entries: list[dict[str, Any]] = []
        self._idempotency: dict[str, str] = {}
        self._entry_by_id = {}
        self._loaded_size = 0
        self._tool_projection = {}
        self._tool_projection_count = 0
        self._projections = {}
        self._type_index = {}
        self._indexed_count = 0
        self._loaded_identity = None
        with self._lock, _file_lock(self.path):
            if self.path.exists():
                self._load()
                if session_id is not None and session_id != self.session_id:
                    raise ValueError(
                        "journal session id does not match requested session"
                    )
            else:
                chosen = session_id or uuid4().hex
                self._validate_session_id(chosen)
                self.session_id = chosen
                self.header = {
                    "type": "session",
                    "version": JOURNAL_VERSION,
                    "id": self.session_id,
                    "createdAt": _now(),
                    "metadata": dict(metadata or {}),
                }
                self._append_line(self.header)

    @classmethod
    def open_in(
        cls, root: str | Path, session_id: str, **kwargs: Any
    ) -> "SessionJournal":
        cls._validate_session_id(session_id)
        return cls(Path(root) / f"{session_id}.jsonl", session_id=session_id, **kwargs)

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not _VALID_SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session id")

    def _load(self) -> None:
        self._projections.clear()
        self._type_index.clear()
        self._indexed_count = 0
        self.recovered_partial_tail = False
        self._tool_projection = {}
        self._tool_projection_count = 0
        self._entries = []
        self._idempotency = {}
        self._entry_by_id = {}
        raw = self.path.read_bytes()
        lines = raw.splitlines(keepends=True)
        values: list[dict[str, Any]] = []
        valid_bytes = 0
        for index, line in enumerate(lines):
            complete = line.endswith((b"\n", b"\r"))
            if index == len(lines) - 1 and not complete:
                self.recovered_partial_tail = True
                break
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError(
                    f"invalid session journal at line {index + 1}: {self.path}"
                )
            if not isinstance(value, dict):
                raise ValueError(
                    f"invalid session entry at line {index + 1}: {self.path}"
                )
            values.append(value)
            valid_bytes += len(line)
        if not values or values[0].get("type") != "session":
            raise ValueError(f"missing session header: {self.path}")
        self.header = values[0]
        if int(self.header.get("version", 0)) != JOURNAL_VERSION:
            raise ValueError(
                f"unsupported session journal version: {self.header.get('version')}"
            )
        self.session_id = str(self.header.get("id", ""))
        self._validate_session_id(self.session_id)
        self._entries = values[1:]
        if self.recovered_partial_tail:
            with self.path.open("r+b") as handle:
                handle.truncate(valid_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        for entry in self._entries:
            self._entry_by_id[entry["id"]] = entry
            key = entry.get("idempotencyKey")
            if key:
                self._idempotency[str(key)] = str(entry["id"])
        self._loaded_size = valid_bytes
        self._loaded_identity = self._identity()

    def _identity(self):
        stat = self.path.stat()
        return (stat.st_ino, stat.st_dev)

    def _refresh(self) -> None:
        """Parse only bytes appended since this instance's last consistent view."""
        try:
            current_size = self.path.stat().st_size
        except FileNotFoundError:
            raise ValueError(f"missing session journal: {self.path}") from None
        identity = self._identity()
        if self._loaded_identity is not None and identity != self._loaded_identity:
            self._load()
            return
        if current_size == self._loaded_size:
            return
        if current_size < self._loaded_size:
            self._load()
            return
        with self.path.open("rb") as handle:
            handle.seek(self._loaded_size)
            raw = handle.read()
        lines = raw.splitlines(keepends=True)
        valid_tail_bytes = 0
        self.recovered_partial_tail = False
        for index, line in enumerate(lines):
            complete = line.endswith((b"\n", b"\r"))
            if index == len(lines) - 1 and not complete:
                self.recovered_partial_tail = True
                break
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError(
                    f"invalid appended session journal data: {self.path}"
                ) from None
            if not isinstance(value, dict):
                raise ValueError(f"invalid appended session entry: {self.path}")
            if value.get("type") == "session":
                raise ValueError(f"unexpected appended session header: {self.path}")
            self._entries.append(value)
            self._entry_by_id[value["id"]] = value
            key = value.get("idempotencyKey")
            if key:
                self._idempotency[str(key)] = str(value["id"])
            valid_tail_bytes += len(line)
        self._loaded_size += valid_tail_bytes
        if self.recovered_partial_tail:
            with self.path.open("r+b") as handle:
                handle.truncate(self._loaded_size)
                handle.flush()
                os.fsync(handle.fileno())

    def _append_line(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self.path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            if _JOURNAL_FSYNC:
                os.fsync(handle.fileno())
        self._loaded_size += len(payload)
        self._loaded_identity = self._identity()

    def append(
        self,
        entry_type: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        parent_id: str | None = None,
        required_event_ids=(),
    ) -> dict[str, Any]:
        if not self.path.is_file():
            raise EvidenceUnavailable("source journal unavailable")
        with self._lock, _file_lock(self.path, create=False):
            self._refresh()
            if any(self._entry_by_id.get(identifier, {}).get("type") in {None, "retention.deleted", "source.deleted"}
                   for identifier in required_event_ids):
                raise EvidenceUnavailable("evidence dependency unavailable or deleted")
            if idempotency_key and idempotency_key in self._idempotency:
                existing_id = self._idempotency[idempotency_key]
                return deepcopy(self._entry_by_id[existing_id])
            entry = {
                "type": entry_type,
                "id": uuid4().hex[:16],
                "parentId": parent_id
                if parent_id is not None
                else (str(self._entries[-1]["id"]) if self._entries else None),
                "timestamp": _now(),
                **deepcopy(dict(payload)),
            }
            if idempotency_key:
                entry["idempotencyKey"] = idempotency_key
            self._append_line(entry)
            self._entries.append(entry)
            self._entry_by_id[entry["id"]] = entry
            if idempotency_key:
                self._idempotency[idempotency_key] = entry["id"]
            return deepcopy(entry)

    @property
    def leaf_id(self) -> str | None:
        with self._lock, _file_lock(self.path):
            self._refresh()
            return str(self._entries[-1]["id"]) if self._entries else None

    def entries(self, *, types=None, tail=None) -> list[dict[str, Any]]:
        with self._lock, _file_lock(self.path):
            self._refresh()
            if types is None:
                rows = self._entries
            else:
                for index in range(self._indexed_count, len(self._entries)):
                    self._type_index.setdefault(
                        self._entries[index].get("type"), []
                    ).append(index)
                self._indexed_count = len(self._entries)
                indices = sorted(
                    i
                    for kind in types
                    for i in (
                        self._type_index.get(kind, ())[-tail:]
                        if tail
                        else self._type_index.get(kind, ())
                    )
                )
                if tail is not None:
                    indices = indices[-tail:] if tail else []
                return [deepcopy(self._entries[i]) for i in indices]
            if tail is not None:
                rows = rows[-tail:] if tail else []
            return deepcopy(rows)

    @property
    def entry_count(self):
        with self._lock, _file_lock(self.path):
            self._refresh()
            return len(self._entries)

    def read_since(self, cursor=None):
        """Return only new rows; an atomic rewrite invalidates every old cursor."""
        with self._lock, _file_lock(self.path):
            self._refresh()
            identity, offset, leaf = cursor or (None, 0, None)
            reset = (
                identity != self._loaded_identity
                or offset > len(self._entries)
                or (offset and self._entries[offset - 1]["id"] != leaf)
            )
            if reset:
                offset = 0
            next_cursor = (
                self._loaded_identity,
                len(self._entries),
                self._entries[-1]["id"] if self._entries else None,
            )
            return next_cursor, deepcopy(self._entries[offset:]), reset

    def project(self, name, initial, reduce, *, select=None):
        """Cache a derived read model on the journal, never on a disposable caller."""
        with self._lock, _file_lock(self.path):
            self._refresh()
            offset, value = (
                self._projections[name] if name in self._projections else (0, initial())
            )
            for row in self._entries[offset:]:
                reduce(value, row)
            self._projections[name] = (len(self._entries), value)
            return deepcopy(select(value) if select else value)

    def rewrite(self, transform):
        """Atomically replace a journal under its writer lock for data erasure."""
        with self._lock, _file_lock(self.path):
            self._refresh()
            rows = transform(deepcopy(self._entries))
            if rows == self._entries:
                return
            temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as handle:
                    for row in [self.header, *rows]:
                        handle.write(
                            (
                                json.dumps(
                                    row, ensure_ascii=False, separators=(",", ":")
                                )
                                + "\n"
                            ).encode("utf-8")
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                self._load()
            finally:
                temporary.unlink(missing_ok=True)

    def tool_activity(self) -> list[dict[str, Any]]:
        from egoassist.core.tool_activity import tool_activity

        with self._lock, _file_lock(self.path):
            self._refresh()
            tool_activity(
                self._entries[self._tool_projection_count :], self._tool_projection
            )
            self._tool_projection_count = len(self._entries)
            return [dict(row) for row in self._tool_projection.values()]

    def append_message(
        self,
        role: str,
        content: str,
        *,
        status: str = "completed",
        source: str,
        response_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if role not in _MESSAGE_ROLES:
            raise ValueError(f"unsupported message role: {role}")
        text = str(content).strip()
        if not text:
            raise ValueError("message content cannot be empty")
        return self.append(
            "message",
            {
                "message": {"role": role, "content": text},
                "status": status,
                "source": source,
                "responseId": response_id,
                "metadata": dict(metadata or {}),
            },
            idempotency_key=idempotency_key,
        )

    def append_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: Mapping[str, Any],
        *,
        status: str = "pending",
        response_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.append(
            "tool_call",
            {
                "callId": str(call_id),
                "name": str(name),
                "arguments": dict(arguments),
                "status": status,
                "responseId": response_id,
                "metadata": dict(metadata or {}),
            },
            idempotency_key=f"tool-call:{call_id}",
        )

    def append_tool_result(
        self,
        call_id: str,
        name: str,
        result: Any,
        *,
        is_error: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.append(
            "tool_result",
            {
                "callId": str(call_id),
                "name": str(name),
                "result": result,
                "isError": bool(is_error),
                "metadata": dict(metadata or {}),
            },
            idempotency_key=f"tool-result:{call_id}",
        )

    def append_state_checkpoint(
        self, state: Mapping[str, Any], *, reason: str
    ) -> dict[str, Any]:
        return self.append("state_checkpoint", {"state": dict(state), "reason": reason})

    def append_retrieval_evidence(
        self,
        query: str,
        evidence: list[Mapping[str, Any]],
        *,
        turn_id: str,
    ) -> dict[str, Any]:
        return self.append(
            "retrieval_evidence",
            {
                "query": str(query),
                "evidence": [dict(item) for item in evidence],
                "trust": "untrusted-data",
                "turnId": str(turn_id),
            },
            idempotency_key=f"retrieval-evidence:{turn_id}",
        )

    def append_turn_context(
        self,
        transcript: str,
        evidence: list[Mapping[str, Any]],
        *,
        turn_id: str,
    ) -> dict[str, Any]:
        return self.append(
            "turn_context",
            {
                "transcript": str(transcript),
                "retrievalEvidence": [dict(item) for item in evidence],
                "trust": "untrusted-user-data",
                "turnId": str(turn_id),
            },
            idempotency_key=f"turn-context:{turn_id}",
        )

    def pending_tool_confirmations(self) -> dict[str, dict[str, Any]]:
        pending: dict[str, dict[str, Any]] = {}
        entries = self.entries(types={"tool_call", "tool_result", "tool_execution", "tool_confirmation"})
        completed = {
            str(entry.get("callId"))
            for entry in entries
            if entry.get("type") in {"tool_result", "tool_execution"}
        }
        origins = {
            str(entry.get("callId")): entry.get("metadata", {}).get("origin")
            for entry in entries
            if entry.get("type") == "tool_call"
        }
        for entry in entries:
            if (
                entry.get("type") == "tool_confirmation"
                and entry.get("status") == "pending"
            ):
                call_id = str(entry.get("callId", ""))
                if call_id and call_id not in completed:
                    pending[call_id] = {
                        "name": str(entry.get("name", "")),
                        "arguments": dict(entry.get("arguments", {})),
                        "generation": None,
                    }
                    origin = entry.get("origin") or origins.get(call_id)
                    if origin:
                        pending[call_id]["origin"] = dict(origin)
        return pending
