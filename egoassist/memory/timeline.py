"""Persistent timeline memory shared by realtime and offline adapters."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

import numpy as np


TIMELINE_SCHEMA_VERSION = 1
MEMORY_KINDS = {
    "fact", "event", "state", "preference", "task-preference",
    "scene", "observation", "tool-effect", "capture", "legacy",
}
RETRIEVABLE_KINDS = MEMORY_KINDS - {"capture"}
MEMORY_STATUSES = {"active", "pending", "consolidated", "superseded", "deleted", "consumed"}
MEMORY_SCOPES = {"case", "participant"}
MEMORY_TIERS = {"short", "long"}

_LATIN = re.compile(r"[a-z0-9]+", re.I)
_CJK = re.compile(r"[\u4e00-\u9fff]")
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


def _day_key(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now().astimezone().date().isoformat()
    parsed = _parse_time(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().date().isoformat()


def normalize_text(text: str) -> str:
    return " ".join(str(text).casefold().split()).strip()


def looks_like_question(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if normalized.endswith(("?", "？")):
        return True
    if normalized.startswith(_QUESTION_PREFIXES):
        return True
    return any(marker in normalized for marker in ("吗", "么", "什么", "哪儿", "哪里", "怎么", "为何"))


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

    def __init__(self, dimension: int = 384) -> None:
        if dimension < 64:
            raise ValueError("memory embedding dimension must be at least 64")
        self.dimension = int(dimension)

    def __call__(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dimension, dtype=np.float32)
        for token in memory_terms(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            raw = int.from_bytes(digest, "little", signed=False)
            index = raw % self.dimension
            sign = -1.0 if raw & (1 << 63) else 1.0
            weight = 1.2 if len(token) >= 3 else 1.0 if len(token) == 2 else 0.55
            vector[index] += sign * weight
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector


class TimelineMemory:
    """One durable timeline with current, short-term, and long-term views."""

    def __init__(
        self,
        path: str | Path,
        *,
        encoder: Callable[[str], Sequence[float] | np.ndarray] | None = None,
        current_window_seconds: float = 600.0,
        short_term_capacity: int = 40,
        event_sink: Callable[..., Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.encoder = encoder or HashingMemoryEncoder()
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
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
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
            """)
            version = connection.execute(
                "SELECT value FROM timeline_meta WHERE key='schema_version'"
            ).fetchone()
            if version and int(version["value"]) != TIMELINE_SCHEMA_VERSION:
                raise ValueError(f"unsupported timeline schema version: {version['value']}")
            connection.execute(
                "INSERT OR REPLACE INTO timeline_meta(key, value) VALUES('schema_version', ?)",
                (str(TIMELINE_SCHEMA_VERSION),),
            )

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
        happened = _iso(happened_at)
        now = _iso(None)
        normalized = normalize_text(text)
        vector = np.asarray(self.encoder(text), dtype=np.float32).reshape(-1)
        if not vector.size or not np.all(np.isfinite(vector)):
            raise ValueError("memory encoder returned an invalid vector")
        norm = float(np.linalg.norm(vector))
        if norm:
            vector = vector / norm
        source_values = list(dict.fromkeys(str(value) for value in source_ids if value))
        card_id = str(card_id or uuid4().hex[:16])
        supersedes_id = None
        with self._lock, self._connect() as connection:
            existing_id = connection.execute(
                "SELECT * FROM memory_cards WHERE id=?", (card_id,),
            ).fetchone()
            if existing_id:
                return self._row_to_card(existing_id)
            if kind not in {"event", "scene", "observation", "tool-effect"}:
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
                    source_ids, status, importance, supersedes, metadata, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    card_id, user_id, case_id, scope, tier, kind, text, normalized,
                    memory_key, happened, _day_key(happened), vector.tobytes(), vector.size,
                    json.dumps(source_values, ensure_ascii=False), status,
                    max(0.0, min(1.0, float(importance))), supersedes_id,
                    json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True), now, now,
                ),
            )
            row = connection.execute("SELECT * FROM memory_cards WHERE id=?", (card_id,)).fetchone()
        card = self._row_to_card(row)
        self._emit("timeline.memory.written", {"card": card})
        return card

    def begin_capture(
        self,
        *,
        user_id: str,
        case_id: str,
        source_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        self.consume_capture(user_id=user_id, case_id=case_id)
        return self.remember(
            "Awaiting the user's next memory value",
            user_id=user_id,
            case_id=case_id,
            scope="case",
            kind="capture",
            tier="short",
            memory_key="pending-explicit-memory-capture",
            source_ids=source_ids,
            status="pending",
            importance=1.0,
            supersede=False,
        ) or {}

    def pending_capture(self, *, user_id: str, case_id: str) -> dict[str, Any] | None:
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
    ) -> dict[str, Any]:
        if scope not in MEMORY_SCOPES:
            raise ValueError("invalid memory scope")
        now = now or utc_now()
        query_normalized = normalize_text(query)
        query_terms = memory_terms(query)
        query_vector = np.asarray(self.encoder(query), dtype=np.float32).reshape(-1)
        if np.linalg.norm(query_vector):
            query_vector = query_vector / np.linalg.norm(query_vector)
        requested_kinds = set(kinds or RETRIEVABLE_KINDS)
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

        wants_recent = any(marker in query_normalized for marker in _RECENT_HINTS)
        wants_today = any(marker in query_normalized for marker in _TODAY_HINTS)
        wants_long = any(marker in query_normalized for marker in _LONG_HINTS)
        today_key = now.astimezone().date().isoformat()
        candidates = []
        for row in rows:
            card = self._row_to_card(row, include_embedding=True)
            happened = _parse_time(card["happenedAt"])
            age_seconds = max(0.0, (now.astimezone(timezone.utc) - happened).total_seconds())
            layer = "current" if age_seconds <= self.current_window_seconds else card["tier"]
            if wants_recent and layer == "long":
                layer_penalty = 0.2
            elif wants_long and layer == "current":
                layer_penalty = 0.06
            else:
                layer_penalty = 0.0
            if wants_today and card["dayKey"] != today_key:
                time_eligible = card["kind"] in {"preference", "fact", "state", "task-preference"}
            else:
                time_eligible = True
            card_terms = memory_terms(card["text"])
            overlap = len(query_terms & card_terms)
            union = max(1, len(query_terms | card_terms))
            lexical = overlap / union if query_terms else 0.0
            vector = card.pop("_embedding")
            semantic = float(np.dot(query_vector, vector)) if query_vector.size == vector.size else 0.0
            semantic = max(0.0, semantic)
            semantic_evidence = (
                not isinstance(self.encoder, HashingMemoryEncoder)
                and semantic >= 0.25
            )
            digit_bonus = 0.12 if set(_DIGITS.findall(query_normalized)) & set(_DIGITS.findall(card["normalizedText"])) else 0.0
            kind_bonus = 0.08 if card["kind"] in {"fact", "state", "tool-effect"} else 0.04 if card["kind"] in {"event", "preference", "task-preference"} else 0.0
            recency = math.exp(-age_seconds / (7 * 86400.0)) * 0.08
            score = max(0.0, 0.62 * semantic + 0.26 * lexical + digit_bonus + kind_bonus + recency - layer_penalty)
            eligible = time_eligible and (overlap > 0 or semantic_evidence)
            reasons = []
            if overlap:
                reasons.append("term-overlap")
            if semantic_evidence:
                reasons.append("embedding-similarity")
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
                "rejectionReasons": [] if eligible else ["no-relevant-evidence" if time_eligible else "time-mismatch"],
                "card": card,
            })
        candidates.sort(
            key=lambda item: (item["score"], item["card"]["happenedAt"]),
            reverse=True,
        )
        selected = [item for item in candidates if item["score"] >= 0.08][:max(1, int(limit))]
        selected_ids = {item["cardId"] for item in selected}
        for item in candidates:
            item["selected"] = item["cardId"] in selected_ids
            if item["score"] and not item["selected"]:
                item["rejectionReasons"].append("outside-top-k")
        return {
            "method": "timeline-hybrid-retrieval-v1",
            "query": str(query),
            "scope": {"user": str(user_id), "case": case_id, "memoryScope": scope},
            "routing": {
                "recent": wants_recent,
                "today": wants_today,
                "longTerm": wants_long,
                "layers": ["current", "short", "long"],
            },
            "candidates": candidates[:20],
            "selected": selected,
            "injection": {"status": "not-injected", "cardIds": []},
            "claimBoundary": "Natural-language memory cards with hashed-embedding and lexical evidence; no answer is inferred by retrieval itself.",
        }

    def consolidate(self, *, user_id: str, case_id: str | None, scope: str) -> dict[str, Any]:
        active_short = self.cards(
            user_id=user_id, case_id=case_id if scope == "case" else None,
            scope=scope, statuses=("active",),
        )
        active_short = [card for card in active_short if card["tier"] == "short" and card["kind"] not in {"capture"}]
        if len(active_short) <= self.short_term_capacity:
            return {"status": "not-needed", "shortCount": len(active_short), "created": []}
        oldest = list(reversed(active_short))[ : max(1, len(active_short) - self.short_term_capacity // 2)]
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for index, card in enumerate(oldest):
            grouping_key = card.get("memoryKey") or f"chronological:{index // 4}"
            groups[(card["kind"], grouping_key)].append(card)
        created = []
        with self._lock:
            for (kind, memory_key), group in groups.items():
                texts = list(dict.fromkeys(card["text"] for card in group))
                summary = texts[-1] if kind in {"fact", "state", "preference", "task-preference"} else "; ".join(texts[-4:])
                consolidation_id = "consolidated-" + hashlib.sha256(
                    (
                        f"{user_id}|{scope}|{case_id or ''}|{kind}|"
                        + "|".join(item["id"] for item in group)
                    ).encode("utf-8")
                ).hexdigest()[:16]
                card = self.remember(
                    summary,
                    user_id=user_id,
                    case_id=case_id,
                    scope=scope,
                    kind=kind,
                    tier="long",
                    memory_key=group[0].get("memoryKey"),
                    happened_at=group[-1]["happenedAt"],
                    source_ids=[item["id"] for item in group],
                    importance=max(item["importance"] for item in group),
                    metadata={"consolidation": "deterministic-card-group-v1"},
                    supersede=False,
                    card_id=consolidation_id,
                )
                if card:
                    created.append(card["id"])
                for child in group:
                    self.update_status(child["id"], "consolidated")
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

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self._event_sink:
            return
        public_payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        self._event_sink(event_type, "timeline-memory", public_payload)
