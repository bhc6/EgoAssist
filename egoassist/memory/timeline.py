"""Persistent timeline memory shared by realtime and offline adapters."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

import numpy as np

from .temporal import ClockContext, query_time_range


class _ClosingConnection:
    """Make SQLite's transaction context also release the Windows file handle."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._connection.__enter__()
        return self._connection

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            self._connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self._connection.close()


TIMELINE_SCHEMA_VERSION = 2
MEMORY_KINDS = {
    "fact", "event", "state", "preference", "task-preference",
    "scene", "observation", "tool-effect", "capture", "legacy",
}
RETRIEVABLE_KINDS = MEMORY_KINDS - {"capture"}
DEFAULT_RETRIEVABLE_KINDS = RETRIEVABLE_KINDS - {"legacy", "scene"}
DEFAULT_RETRIEVAL_MIN_SCORE = 0.18
MEMORY_STATUSES = {"active", "pending", "consolidated", "superseded", "deleted", "consumed"}
MEMORY_SCOPES = {"case", "participant"}
MEMORY_TIERS = {"short", "long"}

_LATIN = re.compile(r"[a-z0-9]+", re.I)
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u309f\u30a0-\u30ff\uac00-\ud7a3]")
_DIGITS = re.compile(r"\d+")
_QUESTION_PREFIXES = (
    "who", "what", "where", "when", "why", "how", "which", "can you", "do you",
    "谁", "什么", "哪里", "哪儿", "何时", "什么时候", "为什么", "怎么", "如何",
    "有没有", "是否", "请问", "帮我看", "帮我查", "记得吗",
)
_RECENT_HINTS = (
    "just now", "a moment ago", "recently", "刚才", "刚刚", "方才", "最近", "现在",
)
_TODAY_HINTS = ("today", "tonight", "今天", "今晚", "今早", "今晨", "今中午")
_LONG_HINTS = (
    "yesterday", "last week", "usually", "every day", "habit", "routine", "以前", "过去",
    "昨天", "上周", "通常", "每天", "习惯", "一直",
)


@dataclass(frozen=True)
class LanguageHints:
    """Language-specific keyword lists for temporal routing and question detection.

    Pass a custom instance to ``TimelineMemory.__init__`` for deployments whose
    primary language is not Chinese or English.  All defaults replicate the
    original bilingual (zh-CN + en) behaviour so existing deployments are
    unaffected.
    """

    question_prefixes: tuple[str, ...] = field(default=_QUESTION_PREFIXES)
    question_markers: tuple[str, ...] = field(
        default=("吗", "么", "什么", "哪儿", "哪里", "怎么", "为何"),
    )
    recent_hints: tuple[str, ...] = field(default=_RECENT_HINTS)
    today_hints: tuple[str, ...] = field(default=_TODAY_HINTS)
    long_hints: tuple[str, ...] = field(default=_LONG_HINTS)


_DEFAULT_HINTS: LanguageHints = LanguageHints()


@dataclass(frozen=True)
class ScoringWeights:
    """Scoring formula coefficients and bonus terms for ``TimelineMemory.retrieve()``.

    All defaults were tuned on a bilingual zh-CN/en dataset with
    ``HashingMemoryEncoder`` (weak semantic signal, ``semantic_coeff``
    contributes near zero in practice).  Adjust when swapping in a production
    semantic encoder or deploying in a new language.
    """

    # Primary mixing coefficients
    semantic_coeff: float = 0.62
    lexical_coeff: float = 0.26

    # Layer-routing penalties
    layer_penalty_recent_long: float = 0.2   # recent query, card is in long layer
    layer_penalty_long_current: float = 0.06  # long-term query, card is current

    # Bonus terms
    digit_overlap_bonus: float = 0.12
    kind_bonus_fact: float = 0.08   # fact / state / tool-effect
    kind_bonus_event: float = 0.04  # event / preference / task-preference
    structured_full_bonus: float = 0.24   # subject + predicate both match
    structured_half_bonus: float = 0.14   # only subject or predicate matches
    today_time_bonus: float = 0.08
    recency_half_life_days: float = 7.0
    recency_max_weight: float = 0.08

    # FTS5 score normalisation (bm25 returns negative values; divide to [0, 1])
    fts_normalization_divisor: float = 10.0

    # Minimum score for a candidate to be included in ``selected``
    min_score: float = DEFAULT_RETRIEVAL_MIN_SCORE


_DEFAULT_WEIGHTS: ScoringWeights = ScoringWeights()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | str | None) -> str:
    if value is None:
        return utc_now().isoformat(timespec="milliseconds")
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _day_key(
    value: datetime | str | None,
    timezone_name: str = "UTC",
    utc_offset_minutes: int = 0,
) -> str:
    if value is None:
        value = utc_now()
    parsed = _parse_time(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    clock = ClockContext.current(
        timezone_name,
        utc_offset_minutes,
        now_utc=parsed.astimezone(timezone.utc),
    )
    return clock.local_now.date().isoformat()


def normalize_identity(value: str | None) -> str | None:
    normalized = normalize_text(value or "")
    return normalized or None


def memory_key_for(subject: str | None, predicate: str | None) -> str | None:
    """Generate entity-qualified keys at the trusted storage boundary."""

    entity = normalize_identity(subject)
    relation = normalize_identity(predicate)
    if not entity or not relation:
        return None
    if relation in {"phone", "telephone", "contact.phone"}:
        return f"contact:{entity}:phone"
    if relation in {"location", "located_at", "object.location"}:
        return f"location:{entity}"
    return f"{relation}:{entity}"


def normalize_text(text: str) -> str:
    # NFC normalization ensures consistent canonical forms for Arabic, Hebrew,
    # and composed Unicode sequences before case-folding.
    return " ".join(unicodedata.normalize("NFC", str(text)).casefold().split()).strip()


def looks_like_question(text: str, hints: LanguageHints | None = None) -> bool:
    """Return True when *text* looks like a question rather than a statement.

    Pass a ``LanguageHints`` instance to add question prefixes/markers for
    languages beyond the default bilingual (zh-CN + en) set.
    """
    _h = hints or _DEFAULT_HINTS
    normalized = normalize_text(text)
    if not normalized:
        return False
    if normalized.endswith(("?", "？")):
        return True
    if normalized.startswith(_h.question_prefixes):
        return True
    return any(marker in normalized for marker in _h.question_markers)


def memory_terms(text: str) -> set[str]:
    normalized = normalize_text(text)
    terms = set(_LATIN.findall(normalized))
    cjk = "".join(_CJK.findall(normalized))
    if len(cjk) == 1:
        terms.add(cjk)
    terms.update(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
    terms.update(cjk[index:index + 3] for index in range(max(0, len(cjk) - 2)))
    return {item for item in terms if item}


class HashingMemoryEncoder:
    """Small deterministic encoder; production adapters may inject a semantic encoder."""

    def __init__(
        self,
        dimension: int = 384,
        *,
        tokenize_fn: Callable[[str], set[str]] | None = None,
        token_weight_fn: Callable[[str], float] | None = None,
    ) -> None:
        """
        Args:
            dimension: embedding vector length (≥ 64).
            tokenize_fn: replace the default ``memory_terms`` tokenizer.  Override
                for scripts where the CJK/Latin extraction yields no tokens
                (Arabic, Thai, Devanagari, etc.).
            token_weight_fn: replace the default length-based weight.  The built-in
                weights (0.55 for len-1, 1.0 for len-2, 1.2 for len≥3) were tuned
                for CJK/Latin; deployments where single-char tokens are morphemically
                meaningful should return a higher weight for ``len(token) == 1``.
        """
        if dimension < 64:
            raise ValueError("memory embedding dimension must be at least 64")
        self.dimension = int(dimension)
        self._tokenize: Callable[[str], set[str]] = tokenize_fn or memory_terms
        self._weight: Callable[[str], float] = token_weight_fn or (
            lambda token: 1.2 if len(token) >= 3 else 1.0 if len(token) == 2 else 0.55
        )

    def __call__(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dimension, dtype=np.float32)
        for token in self._tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "little", signed=False)
            index = raw % self.dimension
            sign = -1.0 if raw & (1 << 63) else 1.0
            vector[index] += sign * self._weight(token)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector


class TimelineMemory:
    """One durable timeline with current, short-term, and long-term views."""

    def __init__(
        self,
        path: str | Path,
        *,
        encoder: Callable[[str], Sequence[float] | np.ndarray] | None = None,
        language_hints: LanguageHints | None = None,
        scoring_weights: ScoringWeights | None = None,
        tokenize_fn: Callable[[str], set[str]] | None = None,
        current_window_seconds: float = 600.0,
        short_term_capacity: int = 40,
        event_sink: Callable[..., Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.encoder = encoder or HashingMemoryEncoder()
        self._hints: LanguageHints = language_hints or _DEFAULT_HINTS
        self._weights: ScoringWeights = scoring_weights or _DEFAULT_WEIGHTS
        # tokenize_fn is used for both query and card-text term extraction in
        # retrieve().  Defaults to module-level memory_terms (CJK + Latin).
        # Override for scripts that the default tokenizer cannot handle.
        self._tokenize: Callable[[str], set[str]] = tokenize_fn or memory_terms
        self.current_window_seconds = float(current_window_seconds)
        self.short_term_capacity = int(short_term_capacity)
        if self.current_window_seconds <= 0 or self.short_term_capacity < 4:
            raise ValueError("invalid timeline memory bounds")
        self._event_sink = event_sink
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return _ClosingConnection(connection)

    def _connect_init(self) -> sqlite3.Connection:
        """Autocommit connection used exclusively by _initialize().

        _initialize() mixes executescript() (which always runs in autocommit) with
        regular execute() calls.  Python's sqlite3 module tracks implicit-transaction
        state at the Python level, and calling executescript() leaves that state
        inconsistent: subsequent DML execute() calls start a new implicit transaction,
        but any FTS5 shadow-table writes done by the rebuild executescript() (autocommit)
        are then re-read from inside that new transaction — which confuses SQLite 3.50+
        and raises "database disk image is malformed".

        isolation_level=None disables Python's implicit transaction management entirely,
        so every statement is committed immediately and the FTS5 shadow tables are always
        in a consistent state for the trigger that fires during the migration UPDATE.
        """
        connection = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        conn = self._connect_init()
        try:
            with self._lock:
                conn.execute("PRAGMA journal_mode=WAL")
                # Step 1: core tables — NO FTS5 yet so migration UPDATE has no triggers
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS timeline_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_cards (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    case_id TEXT,
                    scope TEXT NOT NULL CHECK(scope IN ('case', 'participant')),
                    tier TEXT NOT NULL CHECK(tier IN ('short', 'long')),
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    memory_key TEXT,
                    happened_at TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    recorded_at TEXT,
                    effective_at TEXT,
                    effective_end TEXT,
                    timezone_name TEXT,
                    utc_offset_minutes INTEGER,
                    recurrence TEXT,
                    subject TEXT,
                    predicate TEXT,
                    value TEXT,
                    embedding BLOB NOT NULL,
                    embedding_dim INTEGER NOT NULL,
                    source_ids TEXT NOT NULL,
                    status TEXT NOT NULL,
                    importance REAL NOT NULL,
                    supersedes TEXT,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_cards_owner
                    ON memory_cards(user_id, scope, case_id, status, happened_at DESC);
                CREATE INDEX IF NOT EXISTS memory_cards_key
                    ON memory_cards(user_id, memory_key, status, happened_at DESC);
                CREATE INDEX IF NOT EXISTS memory_cards_tier
                    ON memory_cards(user_id, tier, status, happened_at DESC);
                CREATE TABLE IF NOT EXISTS memory_imports (
                    source_path TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_drafts (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    subject TEXT,
                    predicate TEXT NOT NULL,
                    language TEXT,
                    missing_fields TEXT NOT NULL,
                    source_ids TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_drafts_owner
                    ON memory_drafts(user_id, case_id, status, expires_at DESC);
            """)
                # Step 2: schema migration — runs BEFORE FTS5 exists so no triggers fire.
                version = conn.execute(
                    "SELECT value FROM timeline_meta WHERE key='schema_version'"
                ).fetchone()
                if version and int(version["value"]) not in {1, TIMELINE_SCHEMA_VERSION}:
                    raise ValueError(f"unsupported timeline schema version: {version['value']}")
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(memory_cards)").fetchall()
                }
                migrations = {
                    "recorded_at": "TEXT",
                    "effective_at": "TEXT",
                    "effective_end": "TEXT",
                    "timezone_name": "TEXT",
                    "utc_offset_minutes": "INTEGER",
                    "recurrence": "TEXT",
                    "subject": "TEXT",
                    "predicate": "TEXT",
                    "value": "TEXT",
                }
                for column, declaration in migrations.items():
                    if column not in columns:
                        conn.execute(
                            f"ALTER TABLE memory_cards ADD COLUMN {column} {declaration}"
                        )
                conn.execute(
                    """UPDATE memory_cards
                       SET recorded_at=COALESCE(recorded_at, created_at),
                           effective_at=COALESCE(effective_at, happened_at),
                           timezone_name=COALESCE(timezone_name, 'UTC'),
                           utc_offset_minutes=COALESCE(utc_offset_minutes, 0),
                           recurrence=COALESCE(recurrence, 'null')"""
                )
                conn.execute(
                    "INSERT OR REPLACE INTO timeline_meta(key, value) VALUES('schema_version', ?)",
                    (str(TIMELINE_SCHEMA_VERSION),),
                )
                # Step 3: FTS5 virtual table + triggers — created after migration so the
                # schema is stable and all existing rows have the required columns.
                # Content-table FTS5 requires delete-then-reinsert for updates/deletes;
                # a direct UPDATE on memory_fts is not valid and corrupts shadow tables.
                try:
                    conn.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                        text, normalized_text,
                        content=memory_cards,
                        tokenize='trigram'
                    );
                    CREATE TRIGGER IF NOT EXISTS memory_cards_fts_insert
                        AFTER INSERT ON memory_cards BEGIN
                            INSERT INTO memory_fts(rowid, text, normalized_text)
                            VALUES (new.rowid, new.text, new.normalized_text);
                        END;
                    CREATE TRIGGER IF NOT EXISTS memory_cards_fts_update_before
                        BEFORE UPDATE ON memory_cards BEGIN
                            INSERT INTO memory_fts(memory_fts, rowid, text, normalized_text)
                            VALUES ('delete', old.rowid, old.text, old.normalized_text);
                        END;
                    CREATE TRIGGER IF NOT EXISTS memory_cards_fts_update_after
                        AFTER UPDATE ON memory_cards BEGIN
                            INSERT INTO memory_fts(rowid, text, normalized_text)
                            VALUES (new.rowid, new.text, new.normalized_text);
                        END;
                    CREATE TRIGGER IF NOT EXISTS memory_cards_fts_delete
                        BEFORE DELETE ON memory_cards BEGIN
                            INSERT INTO memory_fts(memory_fts, rowid, text, normalized_text)
                            VALUES ('delete', old.rowid, old.text, old.normalized_text);
                        END;
                    """)
                    # Populate FTS5 from existing rows (first-time or post-migration).
                    fts_count = conn.execute(
                        "SELECT count(*) FROM memory_fts"
                    ).fetchone()[0]
                    row_count = conn.execute(
                        "SELECT count(*) FROM memory_cards"
                    ).fetchone()[0]
                    if fts_count < row_count:
                        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
                except Exception:
                    pass  # FTS5 not compiled in this SQLite build; non-fatal
        finally:
            conn.close()

    def remember(
        self,
        text: str,
        *,
        user_id: str,
        case_id: str | None,
        scope: str,
        kind: str,
        tier: str = "short",
        memory_key: str | None = None,
        happened_at: datetime | str | None = None,
        recorded_at: datetime | str | None = None,
        effective_at: datetime | str | None = None,
        effective_end: datetime | str | None = None,
        timezone_name: str = "UTC",
        utc_offset_minutes: int = 0,
        recurrence: Mapping[str, Any] | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        value: str | None = None,
        source_ids: Iterable[str] = (),
        status: str = "active",
        importance: float = 0.5,
        metadata: Mapping[str, Any] | None = None,
        supersede: bool = True,
        card_id: str | None = None,
    ) -> dict[str, Any] | None:
        text = " ".join(str(text).split()).strip()[:1000]
        if not text:
            return None
        if scope not in MEMORY_SCOPES or tier not in MEMORY_TIERS:
            raise ValueError("invalid memory scope or tier")
        if kind not in MEMORY_KINDS or status not in MEMORY_STATUSES:
            raise ValueError("invalid memory kind or status")
        if scope == "case" and not case_id:
            raise ValueError("case-scoped memory requires case_id")
        user_id = str(user_id).strip()
        case_id = str(case_id).strip() if case_id else None
        if not user_id:
            raise ValueError("memory user_id is required")
        now = _iso(recorded_at)
        effective = _iso(effective_at) if effective_at is not None else (
            _iso(happened_at) if happened_at is not None else None
        )
        effective_stop = _iso(effective_end) if effective_end is not None else None
        happened = effective or now
        normalized_subject = normalize_identity(subject)
        normalized_predicate = normalize_identity(predicate)
        generated_key = memory_key_for(normalized_subject, normalized_predicate)
        if generated_key:
            memory_key = generated_key
        recurrence_json = json.dumps(recurrence, ensure_ascii=False, sort_keys=True) if recurrence else "null"
        normalized = normalize_text(text)
        vector = np.asarray(self.encoder(text), dtype=np.float32).reshape(-1)
        if not vector.size or not np.all(np.isfinite(vector)):
            raise ValueError("memory encoder returned an invalid vector")
        norm = float(np.linalg.norm(vector))
        if norm:
            vector = vector / norm
        source_values = list(dict.fromkeys(str(value) for value in source_ids if value))
        explicit_card_id = card_id is not None
        card_id = str(card_id or uuid4().hex[:16])
        supersedes_id = None
        with self._lock, self._connect() as connection:
            existing_id = connection.execute(
                "SELECT * FROM memory_cards WHERE id=?", (card_id,),
            ).fetchone()
            if existing_id:
                return self._row_to_card(existing_id)
            if not explicit_card_id and kind not in {"event", "scene", "observation", "tool-effect"}:
                duplicate = connection.execute(
                    """SELECT * FROM memory_cards
                       WHERE user_id=? AND scope=? AND COALESCE(case_id, '')=COALESCE(?, '')
                         AND tier=? AND kind=? AND normalized_text=? AND status=?
                       ORDER BY happened_at DESC LIMIT 1""",
                    (user_id, scope, case_id, tier, kind, normalized, status),
                ).fetchone()
                if duplicate:
                    return self._row_to_card(duplicate)
            if supersede and memory_key and status == "active" and kind not in {"event", "scene", "observation"}:
                if scope == "participant":
                    previous = connection.execute(
                        """SELECT id FROM memory_cards
                           WHERE user_id=? AND scope='participant' AND memory_key=?
                             AND status='active'
                           ORDER BY happened_at DESC LIMIT 1""",
                        (user_id, memory_key),
                    ).fetchone()
                else:
                    previous = connection.execute(
                        """SELECT id FROM memory_cards
                           WHERE user_id=? AND scope='case' AND case_id=? AND memory_key=?
                             AND status='active'
                           ORDER BY happened_at DESC LIMIT 1""",
                        (user_id, case_id, memory_key),
                    ).fetchone()
                if previous:
                    supersedes_id = str(previous["id"])
                    connection.execute(
                        "UPDATE memory_cards SET status='superseded', updated_at=? WHERE id=?",
                        (now, supersedes_id),
                    )
            connection.execute(
                """INSERT INTO memory_cards(
                    id, user_id, case_id, scope, tier, kind, text, normalized_text,
                    memory_key, happened_at, day_key, embedding, embedding_dim,
                    source_ids, status, importance, supersedes, metadata, created_at, updated_at,
                    recorded_at, effective_at, effective_end, timezone_name, utc_offset_minutes, recurrence,
                    subject, predicate, value
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    card_id, user_id, case_id, scope, tier, kind, text, normalized,
                    memory_key, happened, _day_key(happened, timezone_name, utc_offset_minutes), vector.tobytes(), vector.size,
                    json.dumps(source_values, ensure_ascii=False), status,
                    max(0.0, min(1.0, float(importance))), supersedes_id,
                    json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True), now, now,
                    now, effective, effective_stop, str(timezone_name or "UTC"),
                    int(utc_offset_minutes), recurrence_json,
                    normalized_subject, normalized_predicate, None if value is None else str(value),
                ),
            )
            row = connection.execute("SELECT * FROM memory_cards WHERE id=?", (card_id,)).fetchone()
        card = self._row_to_card(row)
        self._emit("timeline.memory.written", {"card": card})
        return card

    def begin_draft(
        self,
        *,
        user_id: str,
        case_id: str,
        predicate: str,
        subject: str | None = None,
        missing_fields: Iterable[str] = ("value",),
        language: str | None = None,
        source_ids: Iterable[str] = (),
        lifetime_seconds: float = 300.0,
    ) -> dict[str, Any]:
        relation = normalize_identity(predicate)
        if not relation:
            raise ValueError("memory draft predicate is required")
        now = utc_now()
        draft_id = uuid4().hex[:16]
        missing = sorted({str(item) for item in missing_fields if str(item)}) or ["value"]
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE memory_drafts SET status='cancelled', updated_at=?
                   WHERE user_id=? AND case_id=? AND status='pending'""",
                (_iso(now), str(user_id), str(case_id)),
            )
            connection.execute(
                """INSERT INTO memory_drafts(
                    id, user_id, case_id, subject, predicate, language, missing_fields,
                    source_ids, status, started_at, expires_at, updated_at, metadata
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, '{}')""",
                (
                    draft_id, str(user_id), str(case_id), normalize_identity(subject), relation,
                    str(language or "") or None, json.dumps(missing, ensure_ascii=False),
                    json.dumps([str(item) for item in source_ids if item], ensure_ascii=False),
                    _iso(now), _iso(now + timedelta(seconds=max(1.0, float(lifetime_seconds)))), _iso(now),
                ),
            )
            row = connection.execute("SELECT * FROM memory_drafts WHERE id=?", (draft_id,)).fetchone()
        draft = self._row_to_draft(row)
        self._emit("timeline.memory.draft.started", {"draft": draft})
        return draft

    def update_draft(
        self,
        draft_id: str,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        missing_fields: Iterable[str] | None = None,
        source_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_drafts WHERE id=?", (str(draft_id),)).fetchone()
            if row is None or row["status"] != "pending":
                raise ValueError("pending memory draft not found")
            current_subject = normalize_identity(row["subject"])
            next_subject = normalize_identity(subject) or current_subject
            current_predicate = normalize_identity(row["predicate"])
            next_predicate = normalize_identity(predicate) or current_predicate
            if current_subject and next_subject != current_subject:
                raise ValueError("memory draft subject mismatch")
            if current_predicate and next_predicate != current_predicate:
                raise ValueError("memory draft predicate mismatch")
            sources = json.loads(row["source_ids"])
            sources.extend(str(item) for item in source_ids if item)
            sources = list(dict.fromkeys(sources))
            missing = (
                sorted({str(item) for item in missing_fields if str(item)})
                if missing_fields is not None else json.loads(row["missing_fields"])
            )
            connection.execute(
                """UPDATE memory_drafts SET subject=?, predicate=?, missing_fields=?,
                   source_ids=?, updated_at=? WHERE id=?""",
                (
                    next_subject, next_predicate, json.dumps(missing, ensure_ascii=False),
                    json.dumps(sources, ensure_ascii=False), _iso(None), str(draft_id),
                ),
            )
            updated = connection.execute("SELECT * FROM memory_drafts WHERE id=?", (str(draft_id),)).fetchone()
        draft = self._row_to_draft(updated)
        self._emit("timeline.memory.draft.updated", {"draft": draft})
        return draft

    def pending_draft(self, *, user_id: str, case_id: str) -> dict[str, Any] | None:
        now = _iso(None)
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE memory_drafts SET status='expired', updated_at=?
                   WHERE user_id=? AND case_id=? AND status='pending' AND expires_at<?""",
                (now, str(user_id), str(case_id), now),
            )
            row = connection.execute(
                """SELECT * FROM memory_drafts
                   WHERE user_id=? AND case_id=? AND status='pending'
                   ORDER BY started_at DESC LIMIT 1""",
                (str(user_id), str(case_id)),
            ).fetchone()
        return self._row_to_draft(row) if row else None

    def finish_draft(self, draft_id: str, *, status: str = "consumed") -> dict[str, Any]:
        if status not in {"consumed", "cancelled", "expired"}:
            raise ValueError("invalid memory draft terminal status")
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                "UPDATE memory_drafts SET status=?, updated_at=? WHERE id=? AND status='pending'",
                (status, _iso(None), str(draft_id)),
            )
            if not changed.rowcount:
                raise ValueError("pending memory draft not found")
            row = connection.execute("SELECT * FROM memory_drafts WHERE id=?", (str(draft_id),)).fetchone()
        draft = self._row_to_draft(row)
        self._emit(f"timeline.memory.draft.{status}", {"draft": draft})
        return draft

    @staticmethod
    def draft_accepts(draft: Mapping[str, Any], *, subject: str | None, predicate: str | None) -> bool:
        expected_subject = normalize_identity(draft.get("subject"))
        expected_predicate = normalize_identity(draft.get("predicate"))
        actual_subject = normalize_identity(subject)
        actual_predicate = normalize_identity(predicate)
        return bool(
            actual_predicate
            and actual_predicate == expected_predicate
            and (not expected_subject or actual_subject == expected_subject)
        )

    def begin_capture(
        self,
        *,
        user_id: str,
        case_id: str,
        source_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Compatibility wrapper for callers that do not yet provide a typed draft."""
        return self.begin_draft(
            user_id=user_id,
            case_id=case_id,
            predicate="memory.value",
            missing_fields=("subject", "value"),
            source_ids=source_ids,
        )

    def pending_capture(self, *, user_id: str, case_id: str) -> dict[str, Any] | None:
        draft = self.pending_draft(user_id=user_id, case_id=case_id)
        if draft:
            return {
                **draft,
                "kind": "capture",
                "happenedAt": draft["startedAt"],
                "tier": "short",
                "scope": "case",
            }
        cutoff = _iso(utc_now() - timedelta(minutes=5))
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE memory_cards SET status='consumed', updated_at=?
                   WHERE user_id=? AND case_id=? AND kind='capture' AND status='pending'
                     AND happened_at<?""",
                (_iso(None), user_id, case_id, cutoff),
            )
            row = connection.execute(
                """SELECT * FROM memory_cards
                   WHERE user_id=? AND case_id=? AND kind='capture' AND status='pending'
                     AND happened_at>=? ORDER BY happened_at DESC LIMIT 1""",
                (user_id, case_id, cutoff),
            ).fetchone()
        return self._row_to_card(row) if row else None

    def consume_capture(self, *, user_id: str, case_id: str) -> dict[str, Any] | None:
        draft = self.pending_draft(user_id=user_id, case_id=case_id)
        if draft:
            return self.finish_draft(draft["id"], status="consumed")
        capture = self.pending_capture(user_id=user_id, case_id=case_id)
        if not capture:
            return None
        self.update_status(capture["id"], "consumed")
        return capture

    def update_status(self, card_id: str, status: str) -> dict[str, Any]:
        if status not in MEMORY_STATUSES:
            raise ValueError("invalid memory status")
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                "UPDATE memory_cards SET status=?, updated_at=? WHERE id=?",
                (status, _iso(None), str(card_id)),
            )
            if not changed.rowcount:
                raise ValueError("memory card not found")
            row = connection.execute("SELECT * FROM memory_cards WHERE id=?", (str(card_id),)).fetchone()
        return self._row_to_card(row)

    def activate_pending(self, card_id: str) -> dict[str, Any]:
        now = _iso(None)
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_cards WHERE id=?", (str(card_id),)).fetchone()
            if row is None:
                raise ValueError("memory card not found")
            if row["status"] != "pending":
                raise ValueError("memory card is not pending")
            supersedes_id = None
            if row["memory_key"]:
                previous = connection.execute(
                    """SELECT id FROM memory_cards
                       WHERE user_id=? AND memory_key=? AND status='active' AND id<>?
                         AND scope=? AND COALESCE(case_id, '')=COALESCE(?, '')
                       ORDER BY happened_at DESC LIMIT 1""",
                    (row["user_id"], row["memory_key"], row["id"], row["scope"], row["case_id"]),
                ).fetchone()
                if previous:
                    supersedes_id = str(previous["id"])
                    connection.execute(
                        "UPDATE memory_cards SET status='superseded', updated_at=? WHERE id=?",
                        (now, supersedes_id),
                    )
            connection.execute(
                """UPDATE memory_cards
                   SET status='active', supersedes=?, tier='long', updated_at=? WHERE id=?""",
                (supersedes_id, now, str(card_id)),
            )
            activated = connection.execute("SELECT * FROM memory_cards WHERE id=?", (str(card_id),)).fetchone()
        card = self._row_to_card(activated)
        self._emit("timeline.memory.confirmed", {"card": card})
        return card

    def delete(self, card_id: str) -> dict[str, Any]:
        return self.update_status(card_id, "deleted")

    def export_user(self, user_id: str) -> dict[str, Any]:
        """Export scoped cards and drafts, retaining provenance and temporal fields."""
        with self._lock, self._connect() as connection:
            cards = connection.execute(
                "SELECT * FROM memory_cards WHERE user_id=? ORDER BY happened_at, id", (str(user_id),)
            ).fetchall()
            drafts = connection.execute(
                "SELECT * FROM memory_drafts WHERE user_id=? ORDER BY started_at, id", (str(user_id),)
            ).fetchall()
        return {
            "schema": "egoassist.timeline-memory-export.v1",
            "userId": str(user_id),
            "cards": [self._row_to_card(row) for row in cards],
            "drafts": [self._row_to_draft(row) for row in drafts],
        }

    def delete_user(self, user_id: str) -> dict[str, int]:
        """Hard-delete all participant/case memory for a user; idempotent."""
        with self._lock, self._connect() as connection:
            card_count = connection.execute("SELECT COUNT(*) FROM memory_cards WHERE user_id=?", (str(user_id),)).fetchone()[0]
            draft_count = connection.execute("SELECT COUNT(*) FROM memory_drafts WHERE user_id=?", (str(user_id),)).fetchone()[0]
            connection.execute("DELETE FROM memory_cards WHERE user_id=?", (str(user_id),))
            connection.execute("DELETE FROM memory_drafts WHERE user_id=?", (str(user_id),))
        self._emit("timeline.memory.account_deleted", {"userId": str(user_id), "cards": int(card_count), "drafts": int(draft_count)})
        return {"memoryCards": int(card_count), "memoryDrafts": int(draft_count)}

    def apply_retention(self, *, user_id: str, cutoff: datetime | str) -> dict[str, int]:
        """Remove expired memory cards/drafts for one account with bounded counts."""
        cutoff_value = _iso(cutoff)
        with self._lock, self._connect() as connection:
            cards = connection.execute("DELETE FROM memory_cards WHERE user_id=? AND updated_at<?", (str(user_id), cutoff_value)).rowcount
            drafts = connection.execute("DELETE FROM memory_drafts WHERE user_id=? AND updated_at<?", (str(user_id), cutoff_value)).rowcount
        return {"memoryCards": int(cards or 0), "memoryDrafts": int(drafts or 0)}

    def cards(
        self,
        *,
        user_id: str,
        case_id: str | None = None,
        scope: str | None = None,
        kinds: Iterable[str] | None = None,
        statuses: Iterable[str] = ("active",),
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id=?"]
        values: list[Any] = [str(user_id)]
        if scope:
            if scope not in MEMORY_SCOPES:
                raise ValueError("invalid memory scope")
            clauses.append("scope=?")
            values.append(scope)
        if case_id is not None:
            clauses.append("case_id=?")
            values.append(str(case_id))
        kind_values = list(kinds or [])
        if kind_values:
            clauses.append("kind IN (" + ",".join("?" for _ in kind_values) + ")")
            values.extend(kind_values)
        status_values = list(statuses)
        if status_values:
            clauses.append("status IN (" + ",".join("?" for _ in status_values) + ")")
            values.extend(status_values)
        query = "SELECT * FROM memory_cards WHERE " + " AND ".join(clauses) + " ORDER BY happened_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            values.append(int(limit))
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._row_to_card(row) for row in rows]

    def retrieve(
        self,
        query: str,
        *,
        user_id: str,
        case_id: str | None,
        scope: str,
        limit: int = 5,
        kinds: Iterable[str] | None = None,
        now: datetime | None = None,
        clock: ClockContext | None = None,
        query_spec: Mapping[str, Any] | None = None,
        min_score: float | None = None,
    ) -> dict[str, Any]:
        if scope not in MEMORY_SCOPES:
            raise ValueError("invalid memory scope")
        clock = clock or ClockContext.current(now_utc=now or utc_now())
        now = clock.now_utc
        query_spec = dict(query_spec or {})
        query_subject = normalize_identity(query_spec.get("subject"))
        query_predicate = normalize_identity(query_spec.get("predicate"))
        query_normalized = normalize_text(query)
        query_terms = self._tokenize(query)
        query_vector = np.asarray(self.encoder(query), dtype=np.float32).reshape(-1)
        if np.linalg.norm(query_vector):
            query_vector = query_vector / np.linalg.norm(query_vector)
        requested_kinds = set(DEFAULT_RETRIEVABLE_KINDS if kinds is None else kinds)
        requested_kinds.discard("capture")
        clauses = ["user_id=?", "scope=?", "status='active'"]
        values: list[Any] = [str(user_id), scope]
        if scope == "case":
            clauses.append("case_id=?")
            values.append(str(case_id or ""))
        if requested_kinds:
            clauses.append("kind IN (" + ",".join("?" for _ in requested_kinds) + ")")
            values.extend(sorted(requested_kinds))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_cards WHERE " + " AND ".join(clauses), values,
            ).fetchall()

        # Fast-path: direct memory_key lookup whenever both subject and predicate
        # are provided in the query spec.  memory_key_for() generates a canonical
        # key for any predicate (phone → contact:{e}:phone, location → location:{e},
        # else → {rel}:{e}), so the fast-path generalises to all structured facts.
        pinned_card_ids: set[str] = set()
        if query_subject and query_predicate:
            fast_key = memory_key_for(query_subject, query_predicate)
            if fast_key:
                with self._lock, self._connect() as fast_conn:
                    fast_row = fast_conn.execute(
                        "SELECT * FROM memory_cards "
                        "WHERE user_id=? AND memory_key=? AND status='active' "
                        "ORDER BY happened_at DESC LIMIT 1",
                        [str(user_id), fast_key],
                    ).fetchone()
                if fast_row is not None:
                    fast_card_id = str(fast_row["id"])
                    pinned_card_ids.add(fast_card_id)
                    existing_ids = {r["id"] for r in rows}
                    if fast_card_id not in existing_ids:
                        rows = [fast_row] + list(rows)

        wants_recent = any(marker in query_normalized for marker in self._hints.recent_hints)
        wants_today = any(marker in query_normalized for marker in self._hints.today_hints)
        wants_long = any(marker in query_normalized for marker in self._hints.long_hints)
        today_key = clock.local_now.date().isoformat()
        range_start, range_end = query_time_range(query, clock)

        # Batch FTS5 scoring for all candidates
        fts_scores: dict[str, float] = {}
        if query_terms and rows:
            try:
                fts_query_str = " OR ".join(f'"{term}"' for term in query_terms)
                row_ids = [row["id"] for row in rows]
                placeholders = ",".join("?" * len(row_ids))
                with self._lock, self._connect() as fts_conn:
                    fts_rows = fts_conn.execute(
                        f"""SELECT mc.id, bm25(mf) as score
                            FROM memory_fts mf
                            JOIN memory_cards mc ON mc.rowid = mf.rowid
                            WHERE mf MATCH ?
                              AND mc.id IN ({placeholders})""",
                        [fts_query_str] + row_ids,
                    ).fetchall()
                    for fts_row in fts_rows:
                        # bm25() returns negative scores; more negative = better
                        fts_scores[fts_row[0]] = min(1.0, max(0.0, -float(fts_row[1]) / self._weights.fts_normalization_divisor))
            except Exception:
                pass  # FTS5 not available or query error; fall back to Jaccard

        candidates = []
        for row in rows:
            card = self._row_to_card(row, include_embedding=True)
            happened = _parse_time(card["happenedAt"])
            age_seconds = max(0.0, (now.astimezone(timezone.utc) - happened).total_seconds())
            layer = "current" if age_seconds <= self.current_window_seconds else card["tier"]
            if wants_recent and layer == "long":
                layer_penalty = self._weights.layer_penalty_recent_long
            elif wants_long and layer == "current":
                layer_penalty = self._weights.layer_penalty_long_current
            else:
                layer_penalty = 0.0
            if (wants_today or range_start is not None) and card["dayKey"] != today_key and range_start is None:
                time_eligible = card["kind"] in {"preference", "fact", "state", "task-preference"}
            else:
                time_eligible = True
            if range_start is not None:
                card_start = _parse_time(card["effectiveAt"] or card["happenedAt"])
                card_end = _parse_time(card["effectiveEnd"]) if card.get("effectiveEnd") else None
                upper = range_end or (range_start + timedelta(days=1))
                time_eligible = (
                    card_start < upper and card_end > range_start
                    if card_end is not None
                    else range_start <= card_start < upper
                )
            subject_eligible = not query_subject or normalize_identity(card.get("subject")) == query_subject
            predicate_eligible = not query_predicate or normalize_identity(card.get("predicate")) == query_predicate
            card_terms = self._tokenize(card["text"])
            overlap = len(query_terms & card_terms)
            union = max(1, len(query_terms | card_terms))
            lexical = overlap / union if query_terms else 0.0

            # Use pre-computed FTS5 score if available and better than Jaccard
            fts_score = fts_scores.get(card["id"], 0.0)
            lexical = max(lexical, fts_score)

            vector = card.pop("_embedding")
            semantic = float(np.dot(query_vector, vector)) if query_vector.size == vector.size else 0.0
            semantic = max(0.0, semantic)
            semantic_evidence = (
                not isinstance(self.encoder, HashingMemoryEncoder)
                and semantic >= 0.25
            )
            _w = self._weights
            digit_bonus = _w.digit_overlap_bonus if set(_DIGITS.findall(query_normalized)) & set(_DIGITS.findall(card["normalizedText"])) else 0.0
            kind_bonus = _w.kind_bonus_fact if card["kind"] in {"fact", "state", "tool-effect"} else _w.kind_bonus_event if card["kind"] in {"event", "preference", "task-preference"} else 0.0
            structured_bonus = (
                _w.structured_full_bonus if query_subject and query_predicate and subject_eligible and predicate_eligible
                else _w.structured_half_bonus if (query_subject and subject_eligible) or (query_predicate and predicate_eligible)
                else 0.0
            )
            time_bonus = _w.today_time_bonus if wants_today and time_eligible and card["dayKey"] == today_key else 0.0
            recency = math.exp(-age_seconds / (_w.recency_half_life_days * 86400.0)) * _w.recency_max_weight
            score = max(
                0.0,
                _w.semantic_coeff * semantic + _w.lexical_coeff * lexical + digit_bonus + kind_bonus
                + structured_bonus + time_bonus + recency - layer_penalty,
            )
            structured_match = bool(query_subject or query_predicate) and subject_eligible and predicate_eligible
            fallback_card = card.get("metadata", {}).get("source") == "semantic-memory-fallback"
            fallback_match = fallback_card and lexical >= 0.18
            eligible = time_eligible and subject_eligible and predicate_eligible and (
                structured_match or overlap > 0 or semantic_evidence
            )
            if fallback_match and time_eligible:
                eligible = True
            # Pinned fast-path cards (direct key lookup) always qualify
            if card["id"] in pinned_card_ids:
                score = 1.0
                eligible = True
            reasons = []
            if overlap:
                reasons.append("term-overlap")
            if semantic_evidence:
                reasons.append("embedding-similarity")
            if query_subject and subject_eligible:
                reasons.append("subject-match")
            if query_predicate and predicate_eligible:
                reasons.append("predicate-match")
            if wants_today:
                reasons.append("today-filter" if time_eligible else "outside-today")
            if wants_recent:
                reasons.append("recent-layer-routing")
            elif wants_long:
                reasons.append("long-layer-routing")
            candidates.append({
                "cardId": card["id"],
                "score": round(score if eligible else 0.0, 4),
                "selected": False,
                "layer": layer,
                "kind": card["kind"],
                "reasons": reasons,
                "rejectionReasons": [] if eligible else [
                    "time-mismatch" if not time_eligible else
                    "subject-mismatch" if not subject_eligible else
                    "predicate-mismatch" if not predicate_eligible else
                    "no-relevant-evidence"
                ],
                "card": card,
            })
        candidates.sort(
            key=lambda item: (item["score"], item["card"]["happenedAt"]),
            reverse=True,
        )
        _min_score = min_score if min_score is not None else self._weights.min_score
        selected = [
            item for item in candidates if item["score"] >= _min_score
        ][:max(1, int(limit))]
        selected_ids = {item["cardId"] for item in selected}
        for item in candidates:
            item["selected"] = item["cardId"] in selected_ids
            if item["score"] and not item["selected"]:
                item["rejectionReasons"].append("outside-top-k")
        return {
            "method": "timeline-structured-temporal-retrieval-v2",
            "query": str(query),
            "scope": {"user": str(user_id), "case": case_id, "memoryScope": scope},
            "routing": {
                "recent": wants_recent,
                "today": wants_today,
                "longTerm": wants_long,
                "layers": ["current", "short", "long"],
                "clock": clock.prompt_payload(),
                "querySpec": {"subject": query_subject, "predicate": query_predicate},
            },
            "minimumScore": _min_score,
            "candidates": candidates[:20],
            "selected": selected,
            "injection": {"status": "not-injected", "cardIds": []},
            "claimBoundary": "Entity and civil-time constraints are applied before lexical/embedding ranking; retrieval does not infer an answer.",
        }

    def consolidate(self, *, user_id: str, case_id: str | None, scope: str) -> dict[str, Any]:
        active_short = self.cards(
            user_id=user_id, case_id=case_id if scope == "case" else None,
            scope=scope, statuses=("active",),
        )
        active_short = [card for card in active_short if card["tier"] == "short" and card["kind"] not in {"capture"}]
        if len(active_short) <= self.short_term_capacity:
            return {"status": "not-needed", "shortCount": len(active_short), "created": []}
        oldest = list(reversed(active_short))[:max(1, len(active_short) - self.short_term_capacity // 2)]
        created = []
        with self._lock:
            for source in oldest:
                promotion_id = "promoted-" + hashlib.sha256(
                    f"{user_id}|{scope}|{case_id or ''}|{source['id']}".encode("utf-8")
                ).hexdigest()[:16]
                promoted = self.remember(
                    source["text"],
                    user_id=user_id,
                    case_id=case_id,
                    scope=scope,
                    kind=source["kind"],
                    tier="long",
                    memory_key=source.get("memoryKey"),
                    happened_at=source["happenedAt"],
                    recorded_at=source.get("recordedAt"),
                    effective_at=source.get("effectiveAt"),
                    effective_end=source.get("effectiveEnd"),
                    timezone_name=source.get("timezone") or "UTC",
                    utc_offset_minutes=int(source.get("utcOffsetMinutes") or 0),
                    recurrence=source.get("recurrence"),
                    subject=source.get("subject"),
                    predicate=source.get("predicate"),
                    value=source.get("value"),
                    source_ids=[source["id"], *source.get("sourceIds", [])],
                    importance=source["importance"],
                    metadata={
                        **dict(source.get("metadata") or {}),
                        "consolidation": "deterministic-lossless-promotion-v2",
                    },
                    supersede=False,
                    card_id=promotion_id,
                )
                if promoted:
                    created.append(promoted["id"])
                    self.update_status(source["id"], "consolidated")
        result = {"status": "consolidated", "shortCount": len(active_short), "created": created}
        self._emit("timeline.memory.consolidated", result)
        return result

    def snapshot(self, *, user_id: str, case_id: str | None, scope: str) -> dict[str, Any]:
        if scope == "case" and case_id:
            self.pending_capture(user_id=user_id, case_id=case_id)
        cards = self.cards(
            user_id=user_id, case_id=case_id if scope == "case" else None,
            scope=scope, statuses=("active", "pending"), limit=200,
        )
        now = utc_now()
        counts = {"current": 0, "short": 0, "long": 0}
        for card in cards:
            if (now - _parse_time(card["happenedAt"])).total_seconds() <= self.current_window_seconds:
                counts["current"] += 1
            counts[card["tier"]] += 1
        return {
            "path": str(self.path),
            "count": len(cards),
            "layers": counts,
            "recent": cards[:12],
            "scope": scope,
            "draft": self.pending_draft(user_id=user_id, case_id=case_id) if scope == "case" and case_id else None,
            "retainsRawMedia": False,
        }

    def import_legacy_json(
        self,
        source_path: str | Path,
        *,
        user_id: str,
        case_id: str | None,
        scope: str,
        kind: str,
    ) -> int:
        source = Path(source_path)
        if not source.exists() or not source.is_file():
            return 0
        raw = source.read_bytes()
        fingerprint = hashlib.sha256(raw).hexdigest()
        with self._lock, self._connect() as connection:
            imported = connection.execute(
                "SELECT fingerprint FROM memory_imports WHERE source_path=?", (str(source.resolve()),),
            ).fetchone()
        if imported and imported["fingerprint"] == fingerprint:
            return 0
        payload = json.loads(raw.decode("utf-8"))
        records = payload.get("records", []) if isinstance(payload, dict) else []
        imported_count = 0
        for record in records:
            text = str(record.get("summary") or record.get("preference") or record.get("prompt") or "").strip()
            if not text:
                continue
            record_kind = kind
            if kind == "legacy" and looks_like_question(text):
                continue
            metadata = dict(record)
            memory_key = None
            if kind == "task-preference":
                trigger = record.get("trigger", {})
                memory_key = f"task:{trigger.get('task')}:{trigger.get('step')}"
            legacy_card_id = "legacy-" + hashlib.sha256(
                (
                    f"{source.resolve()}|{user_id}|{scope}|{case_id or ''}|"
                    f"{record.get('id') or imported_count}"
                ).encode("utf-8")
            ).hexdigest()[:16]
            card = self.remember(
                text,
                user_id=user_id,
                case_id=case_id,
                scope=scope,
                kind=record_kind,
                tier="long" if kind == "task-preference" else "short",
                memory_key=memory_key,
                happened_at=record.get("timestamp") or record.get("confirmedAt"),
                source_ids=[record.get("evidenceEventId")] if record.get("evidenceEventId") else [],
                status="active",
                metadata={"legacy": metadata, "importedFrom": str(source)},
                supersede=False,
                card_id=legacy_card_id,
            )
            imported_count += int(card is not None)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO memory_imports(source_path, fingerprint, imported_at) VALUES(?, ?, ?)",
                (str(source.resolve()), fingerprint, _iso(None)),
            )
        return imported_count

    @staticmethod
    def _row_to_card(row: sqlite3.Row, *, include_embedding: bool = False) -> dict[str, Any]:
        card = {
            "id": str(row["id"]),
            "userId": str(row["user_id"]),
            "caseId": row["case_id"],
            "scope": str(row["scope"]),
            "tier": str(row["tier"]),
            "kind": str(row["kind"]),
            "text": str(row["text"]),
            "normalizedText": str(row["normalized_text"]),
            "memoryKey": row["memory_key"],
            "happenedAt": str(row["happened_at"]),
            "dayKey": str(row["day_key"]),
            "recordedAt": str(row["recorded_at"] or row["created_at"]),
            "effectiveAt": row["effective_at"],
            "effectiveEnd": row["effective_end"],
            "timezone": str(row["timezone_name"] or "UTC"),
            "utcOffsetMinutes": int(row["utc_offset_minutes"] or 0),
            "recurrence": json.loads(row["recurrence"] or "null"),
            "subject": row["subject"],
            "predicate": row["predicate"],
            "value": row["value"],
            "sourceIds": json.loads(row["source_ids"]),
            "status": str(row["status"]),
            "importance": float(row["importance"]),
            "supersedes": row["supersedes"],
            "metadata": json.loads(row["metadata"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
        }
        if include_embedding:
            card["_embedding"] = np.frombuffer(
                row["embedding"], dtype=np.float32, count=int(row["embedding_dim"]),
            ).copy()
        return card

    @staticmethod
    def _row_to_draft(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "userId": str(row["user_id"]),
            "caseId": str(row["case_id"]),
            "subject": row["subject"],
            "predicate": str(row["predicate"]),
            "language": row["language"],
            "missingFields": json.loads(row["missing_fields"]),
            "sourceIds": json.loads(row["source_ids"]),
            "status": str(row["status"]),
            "startedAt": str(row["started_at"]),
            "expiresAt": str(row["expires_at"]),
            "updatedAt": str(row["updated_at"]),
            "metadata": json.loads(row["metadata"]),
        }

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self._event_sink:
            return
        public_payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        self._event_sink(event_type, "timeline-memory", public_payload)
