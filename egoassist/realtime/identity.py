"""Local identity and experiment-session ownership backed by SQLite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from .memory_policy import default_memory_policy, normalize_memory_policy
from egoassist.memory.temporal import ClockContext


_USERNAME = re.compile(r"^[\w.-]{3,32}$", re.UNICODE)
_PASSWORD_ROUNDS = 310_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="seconds")


class IdentityStore:
    """Own users, login tokens, and context-session ownership."""

    def __init__(self, path: str | Path, *, login_days: int = 30) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.login_days = max(1, int(login_days))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_sessions_user_id
                    ON login_sessions(user_id);
                CREATE TABLE IF NOT EXISTS context_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    memory_policy TEXT NOT NULL DEFAULT '{}',
                    timezone_name TEXT NOT NULL DEFAULT 'UTC',
                    utc_offset_minutes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS context_sessions_user_id
                    ON context_sessions(user_id, last_seen_at DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(context_sessions)").fetchall()
            }
            if "label" not in columns:
                connection.execute(
                    "ALTER TABLE context_sessions ADD COLUMN label TEXT NOT NULL DEFAULT ''"
                )
            if "memory_policy" not in columns:
                connection.execute(
                    "ALTER TABLE context_sessions ADD COLUMN memory_policy TEXT NOT NULL DEFAULT '{}'"
                )
            if "timezone_name" not in columns:
                connection.execute(
                    "ALTER TABLE context_sessions ADD COLUMN timezone_name TEXT NOT NULL DEFAULT 'UTC'"
                )
            if "utc_offset_minutes" not in columns:
                connection.execute(
                    "ALTER TABLE context_sessions ADD COLUMN utc_offset_minutes INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _normalize_username(username: str) -> tuple[str, str]:
        display = str(username).strip()
        if not _USERNAME.fullmatch(display):
            raise ValueError("username must be 3-32 letters, numbers, dots, dashes, or underscores")
        return display, display.casefold()

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> bytes:
        value = str(password)
        if len(value) < 8 or len(value) > 256:
            raise ValueError("password must be 8-256 characters")
        return hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, _PASSWORD_ROUNDS)

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def register(self, username: str, password: str) -> tuple[dict[str, Any], str]:
        display, username_key = self._normalize_username(username)
        salt = secrets.token_bytes(16)
        password_hash = self._password_hash(password, salt)
        user_id = uuid4().hex[:16]
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, display, username_key, salt, password_hash, _timestamp()),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("username is already registered") from exc
        return self._new_login(user_id, display)

    def login(self, username: str, password: str) -> tuple[dict[str, Any], str]:
        _, username_key = self._normalize_username(username)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username_key = ?", (username_key,),
            ).fetchone()
        if row is None:
            raise ValueError("invalid username or password")
        actual = self._password_hash(password, bytes(row["password_salt"]))
        if not hmac.compare_digest(actual, bytes(row["password_hash"])):
            raise ValueError("invalid username or password")
        return self._new_login(str(row["id"]), str(row["username"]))

    def _new_login(self, user_id: str, username: str) -> tuple[dict[str, Any], str]:
        token = secrets.token_urlsafe(32)
        now = _now()
        expires = now + timedelta(days=self.login_days)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO login_sessions VALUES (?, ?, ?, ?, ?)",
                (self._token_hash(token), user_id, _timestamp(now), _timestamp(expires), _timestamp(now)),
            )
        return {"id": user_id, "username": username}, token

    def authenticate(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        token_hash = self._token_hash(token)
        now = _timestamp()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.username, login_sessions.expires_at
                FROM login_sessions JOIN users ON users.id = login_sessions.user_id
                WHERE login_sessions.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if str(row["expires_at"]) <= now:
                connection.execute("DELETE FROM login_sessions WHERE token_hash = ?", (token_hash,))
                return None
            connection.execute(
                "UPDATE login_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, token_hash),
            )
        return {"id": str(row["id"]), "username": str(row["username"])}

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM login_sessions WHERE token_hash = ?", (self._token_hash(token),),
            )

    def claim_context_session(
        self,
        user_id: str,
        session_id: str | None = None,
        *,
        label: str | None = None,
    ) -> str:
        chosen = str(session_id or uuid4().hex[:16])
        SessionId.validate(chosen)
        case_label = str(label or "").strip()
        if len(case_label) > 80:
            raise ValueError("experiment case label must be at most 80 characters")
        now = _timestamp()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM context_sessions WHERE id = ?", (chosen,),
            ).fetchone()
            if row is not None and str(row["user_id"]) != user_id:
                raise PermissionError("context session belongs to another user")
            if row is None:
                connection.execute(
                    "INSERT INTO context_sessions (id, user_id, label, created_at, last_seen_at, memory_policy) VALUES (?, ?, ?, ?, ?, ?)",
                    (chosen, user_id, case_label, now, now, json.dumps(default_memory_policy(), separators=(",", ":"))),
                )
            else:
                if label is None:
                    connection.execute(
                        "UPDATE context_sessions SET last_seen_at = ? WHERE id = ?",
                        (now, chosen),
                    )
                else:
                    connection.execute(
                        "UPDATE context_sessions SET label = ?, last_seen_at = ? WHERE id = ?",
                        (case_label, now, chosen),
                    )
        return chosen

    def get_context_session(self, user_id: str, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id, user_id, label, created_at, last_seen_at, memory_policy,
                          timezone_name, utc_offset_minutes
                   FROM context_sessions WHERE id = ?""",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError("unknown experiment case")
        if str(row["user_id"]) != user_id:
            raise PermissionError("context session belongs to another user")
        return self._public_context_session(row)

    def list_context_sessions(self, user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, label, created_at, last_seen_at, memory_policy,
                       timezone_name, utc_offset_minutes FROM context_sessions
                WHERE user_id = ? ORDER BY last_seen_at DESC LIMIT ?
                """,
                (user_id, max(1, min(200, int(limit)))),
            ).fetchall()
        return [self._public_context_session(row) for row in rows]

    @staticmethod
    def _public_context_session(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        raw_policy = result.pop("memory_policy", "{}")
        try:
            result["memoryPolicy"] = normalize_memory_policy(json.loads(str(raw_policy) or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["memoryPolicy"] = default_memory_policy()
        result["timezone"] = str(result.pop("timezone_name", "UTC") or "UTC")
        result["utcOffsetMinutes"] = int(result.pop("utc_offset_minutes", 0) or 0)
        return result

    def update_clock_context(
        self,
        user_id: str,
        session_id: str,
        *,
        timezone_name: str,
        utc_offset_minutes: int,
    ) -> dict[str, Any]:
        self.get_context_session(user_id, session_id)
        clock = ClockContext.current(timezone_name, utc_offset_minutes)
        if not -14 * 60 <= int(utc_offset_minutes) <= 14 * 60:
            raise ValueError("invalid UTC offset")
        with self._connect() as connection:
            connection.execute(
                """UPDATE context_sessions
                   SET timezone_name=?, utc_offset_minutes=?, last_seen_at=?
                   WHERE id=? AND user_id=?""",
                (
                    clock.timezone_name, clock.utc_offset_minutes, _timestamp(),
                    str(session_id), str(user_id),
                ),
            )
        return clock.prompt_payload()

    def update_memory_policy(
        self,
        user_id: str,
        session_id: str,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        policy = normalize_memory_policy(value)
        self.get_context_session(user_id, session_id)
        now = _timestamp()
        with self._connect() as connection:
            connection.execute(
                "UPDATE context_sessions SET memory_policy = ?, last_seen_at = ? WHERE id = ? AND user_id = ?",
                (json.dumps(policy, separators=(",", ":")), now, session_id, user_id),
            )
        return policy


class SessionId:
    _pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    @classmethod
    def validate(cls, value: str) -> None:
        if not cls._pattern.fullmatch(value):
            raise ValueError("invalid session id")
