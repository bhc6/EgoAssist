"""Persistent timeline memory shared by realtime and offline adapters."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from .temporal import ClockContext, query_time_range, resolve_time_semantics
from .evidence import evidence_network, ground_context, time_matches
from .grounding import evidence_contract, project_card


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


TIMELINE_SCHEMA_VERSION = 3
MEMORY_KINDS = {
    "fact", "event", "state", "preference", "task-preference",
    "scene", "observation", "tool-effect", "capture", "legacy",
}
RETRIEVABLE_KINDS = MEMORY_KINDS - {"capture"}
DEFAULT_RETRIEVABLE_KINDS = RETRIEVABLE_KINDS - {"legacy", "scene"}
# Minimum lexical score for a candidate to be selected. Calibrated for the
# CLEAN pure-lexical P2 baseline: the score IS the raw lexical similarity
# (Jaccard ∪ BM25 in [0, 1]), with NO structured/recency bias inflating it.
# Genuine multi-term entity matches land at ~0.09–0.13 (e.g. a shared name
# contributes bigram + trigram overlap); true negatives are already zeroed
# upstream by the kind/time/subject eligibility gates and never reach this
# floor. 0.05 admits real lexical evidence with margin below the lowest true
# positive, while rejecting a lone incidental-bigram brush. The old 0.18 was
# a holdover from the hybrid formula (lexical·0.26 + structured + recency) and
# sat ABOVE every genuine pure-lexical positive — hence the under-retrieval.
DEFAULT_RETRIEVAL_MIN_SCORE = 0.05
MEMORY_STATUSES = {"active", "pending", "consolidated", "superseded", "deleted", "consumed"}
MEMORY_SCOPES = {"case", "participant"}
MEMORY_TIERS = {"short", "long"}

_LATIN = re.compile(r"[a-z0-9]+", re.I)
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u309f\u30a0-\u30ff\uac00-\ud7a3]")
_QUESTION_PREFIXES = (
    "who", "what", "where", "when", "why", "how", "which", "can you", "do you",
    "谁", "什么", "哪里", "哪儿", "何时", "什么时候", "为什么", "怎么", "如何",
    "有没有", "是否", "请问", "帮我看", "帮我查", "记得吗",
)
_TODAY_HINTS = ("today", "tonight", "今天", "今晚", "今早", "今晨", "今中午")


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
    today_hints: tuple[str, ...] = field(default=_TODAY_HINTS)


_DEFAULT_HINTS: LanguageHints = LanguageHints()


@dataclass(frozen=True)
class ScoringWeights:
    """Scoring parameters for ``TimelineMemory.retrieve()``.

    P2 (TimelineMemory) is the CLEAN pure-lexical leg of the two-pipeline
    comparison against P1 (HierarchicalMemory, dense vectors). The retrieval
    score IS the lexical similarity (Jaccard ∪ normalised BM25, both in [0, 1]) —
    there are deliberately NO score priors: no semantic/embedding signal, no
    structured-match bonus, no recency decay, no mixing coefficient. Those were
    inductive biases that muddied the contrast against P1's semantic retrieval.

    Subject/predicate/time relevance is enforced upstream as HARD eligibility
    filters (query constraints, not score adjustments), so the two knobs that
    remain here are purely mechanical: how to normalise BM25 into [0, 1], and the
    minimum lexical evidence a candidate needs to be selected.
    """

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


def _subject_identity(value: str | None) -> str | None:
    """Compare a user's first-person possessive without dropping named owners."""
    normalized = normalize_identity(value)
    if not normalized:
        return None
    return re.sub(r"^(?:我的|my\s+)", "", normalized).strip() or normalized


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


class TimelineMemory:
    """One durable timeline with current, short-term, and long-term views."""

    def __init__(
        self,
        path: str | Path,
        *,
        current_window_seconds: float = 600.0,
        short_term_capacity: int = 40,
        event_sink: Callable[..., Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._hints: LanguageHints = _DEFAULT_HINTS
        self._weights: ScoringWeights = _DEFAULT_WEIGHTS
        self._tokenize: Callable[[str], set[str]] = memory_terms
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
                if version and int(version["value"]) not in {1, 2, TIMELINE_SCHEMA_VERSION}:
                    raise ValueError(f"unsupported timeline schema version: {version['value']}")
                columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(memory_cards)").fetchall()
                }
                # v3: drop the write-only embedding columns.  They were persisted on
                # every insert but never read back (retrieval is FTS5 + Jaccard only),
                # so removing them is lossless.  SQLite 3.35+ supports DROP COLUMN.
                for legacy_column in ("embedding", "embedding_dim"):
                    if legacy_column in columns:
                        conn.execute(
                            f"ALTER TABLE memory_cards DROP COLUMN {legacy_column}"
                        )
                        columns.discard(legacy_column)
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
                # v2 → v3: drop the write-only embedding columns.  Retrieval scores
                # purely via FTS5 + Jaccard; the encoder vector was never read back.
                for column in ("embedding", "embedding_dim"):
                    if column in columns:
                        conn.execute(f"ALTER TABLE memory_cards DROP COLUMN {column}")
                conn.execute(
                    """UPDATE memory_cards
                       SET recorded_at=COALESCE(recorded_at, created_at),
                           timezone_name=COALESCE(timezone_name, 'UTC'),
                           utc_offset_minutes=COALESCE(utc_offset_minutes, 0),
                           recurrence=COALESCE(recurrence, 'null')"""
                )
                if "effective_at" not in columns:
                    conn.execute("UPDATE memory_cards SET effective_at=happened_at WHERE effective_at IS NULL")
                conn.execute(
                    "INSERT OR REPLACE INTO timeline_meta(key, value) VALUES('schema_version', ?)",
                    (str(TIMELINE_SCHEMA_VERSION),),
                )
                conn.execute("CREATE INDEX IF NOT EXISTS memory_cards_duplicate_text ON memory_cards(user_id, scope, normalized_text)")
                conn.execute("CREATE INDEX IF NOT EXISTS memory_cards_duplicate_claim ON memory_cards(user_id, scope, subject, predicate, value)")
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
        replaces_card_id: str | None = None,
    ) -> dict[str, Any] | None:
        text = str(text).strip()
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
        now = _iso(recorded_at or happened_at)
        effective = _iso(effective_at) if effective_at is not None else (
            _iso(happened_at) if happened_at is not None else None
        )
        effective_stop = _iso(effective_end) if effective_end is not None else None
        metadata = dict(metadata or {})
        contract = evidence_contract({"text": text, "kind": kind, "recordedAt": now,
            "effectiveAt": effective, "effectiveEnd": effective_stop, "timezone": timezone_name,
            "utcOffsetMinutes": utc_offset_minutes, "recurrence": recurrence,
            "subject": subject, "value": value, "metadata": metadata})
        effective, effective_stop = contract["effectiveAt"], contract["effectiveEnd"]
        recurrence = contract["recurrence"]
        metadata["grounding"] = contract
        happened = effective or now
        normalized_subject = normalize_identity(subject)
        normalized_predicate = normalize_identity(predicate)
        generated_key = memory_key_for(normalized_subject, normalized_predicate)
        if generated_key:
            memory_key = generated_key
        if contract["referenceStatus"] == "needs-clarification":
            memory_key = None
            supersede = False
            if kind in {"fact", "state", "preference"} and status == "active":
                status = "pending"
        recurrence_json = json.dumps(recurrence, ensure_ascii=False, sort_keys=True) if recurrence else "null"
        normalized = normalize_text(text)
        source_values = list(dict.fromkeys(str(source_id) for source_id in source_ids if source_id))
        card_id = str(card_id or uuid4().hex[:16])
        supersedes_id = None
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_id = connection.execute(
                "SELECT * FROM memory_cards WHERE id=?", (card_id,),
            ).fetchone()
            if existing_id:
                if (existing_id["user_id"] != user_id or existing_id["scope"] != scope or
                        existing_id["case_id"] != case_id or existing_id["normalized_text"] != normalized):
                    raise ValueError("memory card id conflicts with another source or owner")
                return self._row_to_card(existing_id)
            # Source retries are idempotent across foreground/recovery writers.
            # Content equality alone cannot merge two distinct occurrences.
            # UNION lets each lookup use its index; OR can scan the owner's history.
            duplicates = connection.execute(
                """SELECT * FROM memory_cards WHERE user_id=? AND scope=?
                   AND COALESCE(case_id, '')=COALESCE(?, '') AND normalized_text=?
                   UNION
                   SELECT * FROM memory_cards WHERE user_id=? AND scope=?
                   AND COALESCE(case_id, '')=COALESCE(?, '') AND subject=? AND predicate=? AND value=?
                   ORDER BY happened_at DESC""",
                (user_id, scope, case_id, normalized,
                 user_id, scope, case_id, normalized_subject, normalized_predicate, value),
            ).fetchall()
            for duplicate in duplicates:
                prior = self._row_to_card(duplicate)
                if any(prior.get(field) != incoming for field, incoming in (
                    ("subject", normalized_subject), ("predicate", normalized_predicate), ("value", value)
                )):
                    continue
                same_source = bool(set(source_values) & set(prior["sourceIds"]))
                same_time = (prior["effectiveAt"], prior["effectiveEnd"], prior["grounding"]["temporalStatus"]) == (
                    effective, effective_stop, contract["temporalStatus"])
                stable_duplicate = (kind not in {"event", "scene", "observation", "tool-effect"}
                    and kind == prior["kind"] and same_time and prior["status"] == status
                    and contract["temporalStatus"] not in {"needs-clarification", "recurring"})
                if replaces_card_id and prior.get("supersedes") != replaces_card_id:
                    continue
                if not (same_source and same_time) and not stable_duplicate:
                    continue
                # Keep every supporting source; a repeated write is not a new observation.
                merged_sources = list(dict.fromkeys(prior["sourceIds"] + source_values))
                connection.execute("UPDATE memory_cards SET source_ids=?, updated_at=? WHERE id=?",
                    (json.dumps(merged_sources, ensure_ascii=False), now, prior["id"]))
                prior["sourceIds"] = merged_sources
                return prior
            if replaces_card_id:
                previous = connection.execute("SELECT * FROM memory_cards WHERE id=?", (replaces_card_id,)).fetchone()
                if (not previous or previous["user_id"] != user_id or previous["scope"] != scope or
                    previous["case_id"] != case_id or previous["status"] != "active" or status != "active" or
                    previous["kind"] not in {"fact", "state", "preference", "event"} or kind not in {"fact", "state", "preference", "event"}):
                    raise ValueError("replacement target must be an active claim in the same user, case and scope")
                supersedes_id = str(previous["id"])
                connection.execute("UPDATE memory_cards SET status='superseded', updated_at=? WHERE id=?", (now, supersedes_id))
                # The same source may also have an unstructured event projection.
                # Supersede that exact projection so it cannot reintroduce a corrected claim.
                old_card = self._row_to_card(previous)
                old_source_text = (old_card["metadata"].get("evidenceContext") or {}).get("sourceText")
                siblings = connection.execute(
                    """SELECT * FROM memory_cards WHERE user_id=? AND scope=?
                       AND COALESCE(case_id, '')=COALESCE(?, '') AND status='active'
                       AND (normalized_text=? OR text=?)""",
                    (user_id, scope, case_id, previous["normalized_text"], old_source_text),
                ).fetchall()
                for sibling in siblings:
                    if set(json.loads(sibling["source_ids"])) & set(old_card["sourceIds"]):
                        connection.execute("UPDATE memory_cards SET status='superseded', updated_at=? WHERE id=?", (now, sibling["id"]))
            if (not replaces_card_id and supersede and memory_key and status == "active"
                    and kind not in {"event", "scene", "observation"} and effective_stop is None
                    and (effective is None or effective <= now)
                    and contract["temporalStatus"] not in {"needs-clarification", "recurring"}):
                if scope == "participant":
                    previous = connection.execute(
                        """SELECT id, happened_at FROM memory_cards
                           WHERE user_id=? AND scope='participant' AND memory_key=?
                             AND status='active' AND effective_end IS NULL
                           ORDER BY happened_at DESC LIMIT 1""",
                        (user_id, memory_key),
                    ).fetchone()
                else:
                    previous = connection.execute(
                        """SELECT id, happened_at FROM memory_cards
                           WHERE user_id=? AND scope='case' AND case_id=? AND memory_key=?
                             AND status='active' AND effective_end IS NULL
                           ORDER BY happened_at DESC LIMIT 1""",
                        (user_id, case_id, memory_key),
                    ).fetchone()
                if previous and previous["happened_at"] > happened:
                    # Late ingestion must not reactivate an older mutable state.
                    status = "superseded"
                elif previous:
                    supersedes_id = str(previous["id"])
                    connection.execute(
                        "UPDATE memory_cards SET status='superseded', updated_at=? WHERE id=?",
                        (now, supersedes_id),
                    )
            connection.execute(
                """INSERT INTO memory_cards(
                    id, user_id, case_id, scope, tier, kind, text, normalized_text,
                    memory_key, happened_at, day_key,
                    source_ids, status, importance, supersedes, metadata, created_at, updated_at,
                    recorded_at, effective_at, effective_end, timezone_name, utc_offset_minutes, recurrence,
                    subject, predicate, value
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    card_id, user_id, case_id, scope, tier, kind, text, normalized,
                    memory_key, happened, _day_key(happened, timezone_name, utc_offset_minutes),
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
            # Retention is based on the memory's event/effective time, not the
            # last projection update. Consolidation and correction must not
            # silently extend an old memory's lifetime.
            cards = connection.execute(
                """DELETE FROM memory_cards
                   WHERE user_id=?
                     AND COALESCE(effective_at, happened_at, recorded_at, created_at)<?""",
                (str(user_id), cutoff_value),
            ).rowcount
            drafts = connection.execute("DELETE FROM memory_drafts WHERE user_id=? AND updated_at<?", (str(user_id), cutoff_value)).rowcount
        return {"memoryCards": int(cards or 0), "memoryDrafts": int(drafts or 0)}

    def forget_sources(self, user_id, source_ids):
        """Erase derived cards and drafts transitively when their evidence expires."""
        from egoassist.core.events import references
        removed = set(source_ids)
        counts = {"memoryCards": 0, "memoryDrafts": 0}
        with self._lock, self._connect() as connection:
            cards = connection.execute("SELECT * FROM memory_cards WHERE user_id=?", (user_id,)).fetchall()
            pending = list(cards)
            while pending:
                kept = []
                changed = False
                for row in pending:
                    card = self._row_to_card(row)
                    if references(card) & removed:
                        removed.add(card["id"])
                        counts["memoryCards"] += connection.execute("DELETE FROM memory_cards WHERE id=? AND user_id=?", (card["id"], user_id)).rowcount
                        changed = True
                    else:
                        kept.append(row)
                if not changed:
                    break
                pending = kept
            drafts = connection.execute("SELECT * FROM memory_drafts WHERE user_id=?", (user_id,)).fetchall()
            for row in drafts:
                if references(self._row_to_draft(row)) & removed:
                    counts["memoryDrafts"] += connection.execute("DELETE FROM memory_drafts WHERE id=? AND user_id=?", (row["id"], user_id)).rowcount
        return removed, counts

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
        include_hard_candidates: bool = False,
        include_candidate_audit: bool = False,
        candidate_card_ids: Iterable[str] | None = None,
        ranking_query: str | None = None,
    ) -> dict[str, Any]:
        if scope not in MEMORY_SCOPES:
            raise ValueError("invalid memory scope")
        started = time.perf_counter()
        clock = clock or ClockContext.current(now_utc=now or utc_now())
        now = clock.now_utc
        query_spec = dict(query_spec or {})
        query_subject = normalize_identity(query_spec.get("subject"))
        query_predicate = normalize_identity(query_spec.get("predicate"))
        query_context = ground_context(query_spec.get("context"), query, clock)
        query_entities = [normalize_identity(item) for item in query_context["entities"]]
        query_times = [item for item in query_context["times"] if item["status"] == "resolved"]
        time_variants = [query_times]
        query_temporal = resolve_time_semantics(query, clock, kind=None)
        broad_history = bool(re.search(r"刚才|之前|以前|\b(?:earlier|recently|just now)\b", query, re.I)) and not query_context["times"] and not query_temporal["dateExplicit"]
        temporal_status = "resolved" if query_times or query_temporal["effectiveAt"] else "not-requested"
        if any(item["status"] != "resolved" for item in query_context["times"]) or (
            not query_context["times"] and not broad_history and query_temporal["status"] == "needs-clarification"
        ):
            temporal_status = "needs-clarification"
        query_normalized = normalize_text(query)
        query_terms = self._tokenize(query if ranking_query is None else ranking_query)
        requested_kinds = set(DEFAULT_RETRIEVABLE_KINDS if kinds is None else kinds)
        requested_kinds.discard("capture")
        clauses = ["user_id=?", "scope=?", "status='active'"]
        values: list[Any] = [str(user_id), scope]
        if scope == "case":
            clauses.append("case_id=?")
            values.append(str(case_id or ""))
        # An empty allowlist must not widen the query to every kind (including
        # raw captures). Apply the same scope to ranking and canonical pinning.
        clauses.append("kind IN (" + ",".join("?" for _ in requested_kinds) + ")" if requested_kinds else "0")
        values.extend(sorted(requested_kinds))
        from egoassist.core.settings import integer
        candidate_limit = integer("EGOASSIST_MEMORY_CANDIDATE_LIMIT", 256, minimum=32, maximum=4096)
        # External retrievers nominate IDs, never eligibility. All existing
        # scope filters and per-card grounding checks still apply below.
        if candidate_card_ids is not None:
            nominated = list(dict.fromkeys(str(sid) for sid in candidate_card_ids))
            if len(nominated) > candidate_limit:
                raise ValueError("external candidate IDs exceed the configured candidate limit")
            clauses.append("id IN (" + ",".join("?" for _ in nominated) + ")" if nominated else "0")
            values.extend(nominated)
        where = " AND ".join(clauses)
        with self._lock, self._connect() as connection:
            total_candidates = connection.execute("SELECT COUNT(*) FROM memory_cards WHERE " + where, values).fetchone()[0]
            if total_candidates <= candidate_limit:
                rows = connection.execute("SELECT * FROM memory_cards WHERE " + where, values).fetchall()
            else:
                candidates_by_id = {}
                fast_key = memory_key_for(query_subject, query_predicate) if query_subject and query_predicate else None
                if fast_key:
                    for row in connection.execute("SELECT * FROM memory_cards WHERE " + where +
                            " AND memory_key=? ORDER BY happened_at DESC LIMIT 1", [*values, fast_key]):
                        candidates_by_id[row["id"]] = row
                if query_terms:
                    try:
                        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in query_terms)
                        scoped = " AND ".join("mc." + clause if clause != "0" else clause for clause in clauses)
                        for row in connection.execute("SELECT mc.* FROM memory_fts CROSS JOIN memory_cards mc ON mc.rowid=memory_fts.rowid "
                                "WHERE memory_fts MATCH ? AND " + scoped + " ORDER BY bm25(memory_fts) LIMIT ?",
                                [match, *values, candidate_limit * 3 // 4]):
                            candidates_by_id[row["id"]] = row
                    except sqlite3.OperationalError:
                        pass
                start, end = query_time_range(query, clock)
                if start is not None:
                    for row in connection.execute("SELECT * FROM memory_cards WHERE " + where +
                            " AND happened_at>=? AND happened_at<? ORDER BY happened_at DESC LIMIT ?",
                            [*values, start.isoformat(), (end or start + timedelta(days=1)).isoformat(), candidate_limit // 4]):
                        if len(candidates_by_id) < candidate_limit:
                            candidates_by_id[row["id"]] = row
                for row in connection.execute("SELECT * FROM memory_cards WHERE " + where +
                        " ORDER BY happened_at DESC LIMIT ?", [*values, candidate_limit]):
                    if len(candidates_by_id) >= candidate_limit:
                        break
                    candidates_by_id[row["id"]] = row
                rows = list(candidates_by_id.values())

        # Canonical-key pinning uses the already scoped candidates. It is a
        # ranking rule, not a second query that can bypass the kind allowlist.
        pinned_card_ids: set[str] = set()
        if query_subject and query_predicate:
            fast_key = memory_key_for(query_subject, query_predicate)
            if fast_key:
                fast_row = max((row for row in rows if row["memory_key"] == fast_key),
                               key=lambda row: row["happened_at"], default=None)
                if fast_row is not None:
                    pinned_card_ids.add(str(fast_row["id"]))

        wants_today = any(marker in query_normalized for marker in self._hints.today_hints)
        today_key = clock.local_now.date().isoformat()
        range_start, range_end = query_time_range(query, clock)
        if query_context["times"]:
            range_start, range_end = None, None

        # Batch FTS5 scoring for all candidates
        fts_scores: dict[str, float] = {}
        if query_terms and rows:
            try:
                fts_query_str = " OR ".join(f'"{term}"' for term in query_terms)
                scoped_clauses = ["mc." + clause if clause != "0" else clause for clause in clauses]
                with self._lock, self._connect() as fts_conn:
                    # bm25() must reference the FTS5 table by its real name, not a
                    # SQL alias — `bm25(mf)` raises "no such column: mf".
                    # CROSS JOIN keeps FTS as the driving table; allowing SQLite
                    # to drive this join from scoped cards repeats MATCH work.
                    fts_rows = fts_conn.execute(
                        f"""SELECT mc.id, bm25(memory_fts) as score
                            FROM memory_fts
                            CROSS JOIN memory_cards mc ON mc.rowid = memory_fts.rowid
                            WHERE memory_fts MATCH ?
                              AND {" AND ".join(scoped_clauses)}
                            ORDER BY bm25(memory_fts) LIMIT ?""",
                        [fts_query_str] + values + [candidate_limit],
                    ).fetchall()
                    for fts_row in fts_rows:
                        # bm25() returns negative scores; more negative = better
                        fts_scores[fts_row[0]] = min(1.0, max(0.0, -float(fts_row[1]) / self._weights.fts_normalization_divisor))
            except Exception:
                pass  # FTS5 not available or query error; fall back to Jaccard

        candidates = []
        for row in rows:
            card = self._row_to_card(row)
            happened = _parse_time(card["happenedAt"])
            age_seconds = max(0.0, (now.astimezone(timezone.utc) - happened).total_seconds())
            # `layer` is a DERIVED retrieval-time axis, not stored state: a recent
            # card surfaces as "current"; otherwise it falls back to its persisted
            # `tier` (short/long). The two persisted axes are `tier` (this card's
            # consolidation level) and `kind` (its semantic type).
            layer = "current" if happened <= now and age_seconds <= self.current_window_seconds else card["tier"]
            if (wants_today or range_start is not None) and card["dayKey"] != today_key and range_start is None:
                time_eligible = False
            else:
                time_eligible = True
            if range_start is not None:
                card_start = _parse_time(card["effectiveAt"]) if card.get("effectiveAt") else None
                card_end = _parse_time(card["effectiveEnd"]) if card.get("effectiveEnd") else None
                upper = range_end or (range_start + timedelta(days=1))
                time_eligible = card_start is not None and (
                    card_start < upper and card_end > range_start
                    if card_end is not None
                    else range_start <= card_start < upper
                )
            evidence = card.get("metadata", {}).get("evidenceContext", {})
            card_times = [item for item in evidence.get("times", []) if item.get("start")]
            temporal_support = "not-requested"
            if any(variant for variant in time_variants):
                temporal_support = "unresolved"
                time_eligible = False
                if card_times:
                    def matches(variant):
                        return all(any(time_matches(requested, item) for item in card_times
                            if normalize_identity(item.get("relation")) == normalize_identity(requested["relation"]))
                            for requested in variant)
                    time_eligible = any(matches(variant) for variant in time_variants if variant)
                    temporal_support = "matching-references" if time_eligible else "conflicting-references"
                elif card.get("effectiveAt"):
                    time_eligible = any(len(variant) == 1 and time_matches(variant[0], {
                        "start": card["effectiveAt"], "end": card.get("effectiveEnd"),
                    }) for variant in time_variants if variant)
                    temporal_support = "legacy-event-time" if time_eligible else "conflicting-event-time"
            if temporal_status == "needs-clarification":
                time_eligible = False
            if (range_start is not None or query_times) and card["grounding"]["recurrence"]:
                time_eligible = False
            reference_eligible = card["grounding"]["referenceStatus"] != "needs-clarification"
            card_entities = {normalize_identity(item) for item in evidence.get("entities", [])}
            entity_eligible = all(item in card_entities or item in normalize_identity(card["text"])
                                  for item in query_entities)
            scene_eligible = not query_context["scene"] or (
                normalize_identity(query_context["scene"]) in normalize_identity(
                    str(evidence.get("scene") or "") + " " + card["text"]
                )
            )
            subject_eligible = not query_subject or _subject_identity(card.get("subject")) == _subject_identity(query_subject)
            predicate_eligible = not query_predicate or normalize_identity(card.get("predicate")) == query_predicate
            # A router can join entity and relation into one subject label.
            # Accept only that exact composition, never a partial entity match.
            if query_subject and not query_predicate and card.get("subject") and card.get("predicate"):
                subject_eligible = subject_eligible or _subject_identity(query_subject) == _subject_identity(
                    str(card["subject"]) + str(card["predicate"]))
            if card["kind"] == "event":
                # An interaction can contain several people and relations;
                # its evidence is not exhausted by a single extracted triple.
                event_text = normalize_identity(card["text"])
                subject_eligible = subject_eligible or query_subject in card_entities or bool(query_subject and query_subject in event_text)
                predicate_eligible = predicate_eligible or bool(query_predicate and query_predicate in event_text)
            # Verbatim fallback cards have no extracted entity/relation. Treat
            # their placeholder as unknown, allowing only constraints actually
            # present in the saved text; never relax a known structured mismatch.
            if normalize_identity(card.get("predicate")) == "verbatim":
                verbatim = normalize_identity(card["text"])
                subject_eligible = not query_subject or query_subject in verbatim
                predicate_eligible = not query_predicate or query_predicate in verbatim
            # Ineligible rows remain in the audit, but cannot contribute a score.
            # Avoid tokenizing their text on every structured lookup.
            card_terms = self._tokenize(card["text"]) if (
                time_eligible and subject_eligible and predicate_eligible and entity_eligible and scene_eligible
            ) else set()
            overlap = len(query_terms & card_terms)
            union = max(1, len(query_terms | card_terms))
            lexical = overlap / union if query_terms else 0.0

            # Use pre-computed FTS5 score if available and better than Jaccard
            fts_score = fts_scores.get(card["id"], 0.0)
            lexical = max(lexical, fts_score)

            # P2 is a CLEAN pure-lexical baseline: the score IS the lexical
            # similarity (Jaccard ∪ BM25, already normalised to [0, 1]). No
            # structured-match bonus, no recency decay, no mixing coefficient —
            # those were inductive biases that muddied the contrast against P1's
            # dense-vector retrieval. Subject/predicate/time remain as HARD
            # eligibility filters below (query constraints, not score priors).
            score = max(0.0, lexical)
            structured_match = bool(query_subject or query_predicate) and subject_eligible and predicate_eligible
            eligible = reference_eligible and time_eligible and subject_eligible and predicate_eligible and entity_eligible and scene_eligible and (
                structured_match or overlap > 0
            )
            # Pinned fast-path cards (direct key lookup) qualify on the strength of
            # their canonical structured key, bypassing the lexical-overlap floor —
            # but they must still fall inside any time window the query asked about.
            # A canonical fact recorded outside the asked-about range is not an
            # answer to a time-scoped question (e.g. "did I take my meds at 3pm?"
            # must not surface the 4pm dose just because it shares the same key).
            if card["id"] in pinned_card_ids and reference_eligible and time_eligible and subject_eligible and predicate_eligible and entity_eligible and scene_eligible:
                score = 1.0
                eligible = True
            reasons = []
            if overlap:
                reasons.append("term-overlap")
            if query_subject and subject_eligible:
                reasons.append("subject-match")
            if query_predicate and predicate_eligible:
                reasons.append("predicate-match")
            if wants_today:
                reasons.append("today-filter" if time_eligible else "outside-today")
            candidates.append({
                "cardId": card["id"],
                "hardEligible": bool(reference_eligible and time_eligible and subject_eligible
                                     and predicate_eligible and entity_eligible and scene_eligible),
                "score": round(score if eligible else 0.0, 4),
                "selected": False,
                "layer": layer,
                "kind": card["kind"],
                "temporalSupport": temporal_support,
                "reasons": reasons,
                "rejectionReasons": [] if eligible else [
                    "unresolved-reference" if not reference_eligible else
                    "time-mismatch" if not time_eligible else
                    "subject-mismatch" if not subject_eligible else
                    "predicate-mismatch" if not predicate_eligible else
                    "entity-mismatch" if not entity_eligible else
                    "scene-mismatch" if not scene_eligible else
                    "no-relevant-evidence"
                ],
                "card": card,
            })
        candidates.sort(
            key=lambda item: (item["score"], item["card"]["happenedAt"]),
            reverse=True,
        )
        _min_score = min_score if min_score is not None else self._weights.min_score
        selected = []
        seen_evidence = set()
        for item in candidates:
            if item["rejectionReasons"] or item["score"] < _min_score:
                continue
            card = item["card"]
            # Legacy duplicate rows stay auditable, but cannot fill the evidence budget.
            identity = (card["normalizedText"], card.get("subject"), card.get("predicate"),
                card.get("value"), card["effectiveAt"], card["effectiveEnd"],
                tuple(sorted(card["sourceIds"])) if card["sourceIds"] else card["id"])
            if identity in seen_evidence:
                item["rejectionReasons"].append("duplicate-evidence")
                continue
            seen_evidence.add(identity)
            if len(selected) < max(1, int(limit)):
                selected.append(item)
        selected_ids = {item["cardId"] for item in selected}
        for item in candidates:
            item["selected"] = item["cardId"] in selected_ids
            if item["score"] and not item["selected"]:
                item["rejectionReasons"].append("outside-top-k")
        return {
            "method": "timeline-structured-temporal-retrieval-v2",
            "query": str(query),
            "rankingQuery": query if ranking_query is None else ranking_query,
            "scope": {"user": str(user_id), "case": case_id, "memoryScope": scope},
            "routing": {
                "today": wants_today,
                "layers": ["current", "short", "long"],
                "clock": clock.prompt_payload(),
                "querySpec": {"subject": query_subject, "predicate": query_predicate},
                "evidenceContext": query_context,
                "temporalStatus": temporal_status,
            },
            "minimumScore": _min_score,
            "timing": {"retrievalMs": round((time.perf_counter() - started) * 1000, 3),
                       "scannedCards": len(rows), "totalCandidates": total_candidates,
                       "candidateLimit": candidate_limit, "candidatesTruncated": total_candidates > len(rows)},
            "candidates": candidates[:20],
            **({"hardCandidates": [item for item in candidates if item["hardEligible"]]}
               if include_hard_candidates else {}),
            **({"candidateAudit": candidates} if include_candidate_audit else {}),
            "selected": selected,
            "injection": {"status": "not-injected", "cardIds": []},
            "scoreMeaning": "lexical-ranking-or-exact-key-priority",
            "calibratedProbability": None,
            "claimBoundary": "Source-grounded entity and time constraints precede lexical ranking. Unknown temporal roles remain unresolved. Scores are not truth probabilities.",
        }

    def promote_to_long(self, *, user_id: str, case_id: str | None, scope: str) -> dict[str, Any]:
        """Losslessly promote the oldest 'short' cards to the 'long' tier.

        Change the tier atomically in place. IDs, dates and sources remain stable;
        a retry cannot create another occurrence or invalidate its own source.
        """
        active_short = self.cards(
            user_id=user_id, case_id=case_id if scope == "case" else None,
            scope=scope, statuses=("active",),
        )
        active_short = [card for card in active_short if card["tier"] == "short" and card["kind"] not in {"capture"}]
        if len(active_short) <= self.short_term_capacity:
            return {"status": "not-needed", "shortCount": len(active_short), "created": []}
        oldest = list(reversed(active_short))[:max(1, len(active_short) - self.short_term_capacity // 2)]
        created = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for source in oldest:
                updated = connection.execute(
                    "UPDATE memory_cards SET tier='long', updated_at=? WHERE id=? AND status='active' AND tier='short'",
                    (_iso(None), source["id"]),
                )
                if updated.rowcount:
                    created.append(source["id"])
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
        clauses = ["user_id=?", "scope=?", "status IN ('active', 'pending')"]
        values = [user_id, scope]
        if scope == "case":
            clauses.append("case_id=?")
            values.append(case_id or "")
        with self._lock, self._connect() as connection:
            aggregate = connection.execute("SELECT COUNT(*), SUM(tier='short'), SUM(tier='long'), SUM(happened_at>=?) FROM memory_cards WHERE " + " AND ".join(clauses),
                [(now - timedelta(seconds=self.current_window_seconds)).isoformat(), *values]).fetchone()
        counts = {"short": aggregate[1] or 0, "long": aggregate[2] or 0, "current": aggregate[3] or 0}
        return {
            "path": str(self.path),
            "count": aggregate[0],
            "layers": counts,
            "recent": cards[:12],
            "evidenceNetwork": {**evidence_network([card for card in cards if card["status"] == "active"]),
                                "cardLimit": 200, "scope": scope},
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
    def _row_to_card(row: sqlite3.Row) -> dict[str, Any]:
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
        return project_card(card)

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
