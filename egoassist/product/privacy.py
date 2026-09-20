"""Account privacy governance and outcome evidence.

This module is deliberately an orchestration layer.  The product database,
IdentityStore, and TimelineMemory remain the owners of their own records;
privacy operations call those owners and emit only bounded deletion evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from egoassist.core.contracts import ProviderDisclosure

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")
_SECRET = re.compile(r"(?i)\b(?:sk|api|token|secret)[-_ ]?(?:key|token)?\s*[:=]\s*[^\s,;]+")
_CN_LABEL = re.compile(r"(姓名|联系人|患者|住址|地址)\s*[:：]\s*([^,，;；\n]+)")


def redact_text(value: str) -> str:
    """Redact common and Chinese/domain-labelled PII at observability boundaries."""
    text = str(value)
    text = _SECRET.sub(lambda match: match.group(0).split(":", 1)[0].split("=", 1)[0] + ":[REDACTED]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[PHONE]", text)
    text = _ID_CARD.sub("[ID_CARD]", text)
    return _CN_LABEL.sub(lambda match: f"{match.group(1)}:[REDACTED]", text)


def redact_pii(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if any(marker in key_text.casefold() for marker in ("secret", "token", "password", "api_key", "apikey")):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = redact_pii(item)
        return result
    if isinstance(value, list):
        return [redact_pii(item) for item in value]
    if isinstance(value, tuple):
        return [redact_pii(item) for item in value]
    return value


def provider_disclosures(*, ambient: bool = False) -> list[dict[str, Any]]:
    """Truthful, user-facing disclosure for every configured data boundary.

    The ambient entry is appended only when ambient observation is actually
    active, so the disclosure describes the running system rather than a
    capability the deployment may never use.
    """
    values = [
        ProviderDisclosure(
            provider="realtime-model",
            data_categories=["microphone audio", "camera frames", "transcript", "turn metadata"],
            purposes=["speech recognition", "dialogue response"],
            retention="provider policy; question audio clips and frame evidence are retained in the account journal media directory until account deletion",
            user_controls=["stop realtime", "delete account", "export account"],
        ),
        ProviderDisclosure(
            provider="structured-vision-model",
            data_categories=["selected video frames", "question context", "checkpoint context"],
            purposes=["shared scene description", "visual answers", "hazard and progress evidence"],
            retention="observed and question-time frames are stored as checksum-addressed account evidence",
            user_controls=["disable camera", "reject evidence", "delete account"],
        ),
        ProviderDisclosure(
            provider="local-speaker-model",
            data_categories=["question audio", "speaker embeddings", "account-confirmed names and frame references"],
            purposes=["speaker segmentation and acoustic matching; not biometric authentication"],
            retention="account-owned journal and media; mixed-speaker segments may be sent to Qwen ASR for transcription",
            user_controls=["stop realtime", "export account", "delete account"],
        ),
        ProviderDisclosure(
            provider="human-help-connector",
            data_categories=["explicitly previewed message", "selected context"],
            purposes=["human assistance"],
            retention="connector policy; sent only after explicit consent",
            user_controls=["preview", "cancel", "delete account"],
        ),
    ]
    if ambient:
        values.append(
            ProviderDisclosure(
                provider="ambient-observation-model",
                data_categories=[
                    "periodically sampled video frames while ambient mode is on",
                    "watchlist activity and location labels",
                ],
                purposes=["passive habit observation", "observation-gated reminding"],
                retention="sampled images, captured reference context and derived observations are retained as account evidence until the observation retention policy or account deletion removes them",
                user_controls=[
                    "turn off ambient mode",
                    "revoke ambient consent",
                    "delete account",
                    "export account",
                ],
            )
        )
    return [item.model_dump(mode="json") for item in values]


def transport_security_summary() -> dict[str, Any]:
    """Expose transport truth without implying TLS where the deployment lacks it."""
    configured = os.getenv("EGOASSIST_PUBLIC_BASE_URL", "http://127.0.0.1")
    loopback = configured.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
    return {
        "publicBaseUrl": configured,
        "tlsRequiredOutsideLoopback": True,
        "transportStatus": "loopback-only" if loopback else ("tls" if configured.startswith("https://") else "misconfigured"),
        "atRest": "encrypted-volume-required; export envelopes support Fernet",
    }


def encrypt_json(value: Mapping[str, Any], key: bytes | str) -> bytes:
    raw_key = key.encode("ascii") if isinstance(key, str) else key
    return Fernet(raw_key).encrypt(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def decrypt_json(token: bytes, key: bytes | str) -> dict[str, Any]:
    raw_key = key.encode("ascii") if isinstance(key, str) else key
    try:
        parsed = json.loads(Fernet(raw_key).decrypt(token).decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid encrypted export") from exc
    if not isinstance(parsed, dict):
        raise ValueError("encrypted export must contain an object")
    return parsed


@dataclass(frozen=True)
class OutcomeRecord:
    """One real outcome with an explicit denominator and provenance."""

    outcome_id: str
    kind: str
    expected: str
    observed: str
    source: str
    user_id: str | None = None
    session_id: str | None = None
    acknowledged: bool = False
    latency_ms: float | None = None


class OutcomeLedger:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._records: list[OutcomeRecord] = []
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    self._records.append(OutcomeRecord(**json.loads(line)))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue

    def record(self, record: OutcomeRecord) -> OutcomeRecord:
        if any(item.outcome_id == record.outcome_id for item in self._records):
            return next(item for item in self._records if item.outcome_id == record.outcome_id)
        self._records.append(record)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n")
        return record

    def records_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [asdict(item) for item in self._records if item.user_id == str(user_id)]

    def delete_user(self, user_id: str) -> int:
        before = len(self._records)
        self._records = [item for item in self._records if item.user_id != str(user_id)]
        if self.path:
            payload = "".join(json.dumps(asdict(item), ensure_ascii=False, sort_keys=True) + "\n" for item in self._records)
            self.path.write_text(payload, encoding="utf-8")
        return before - len(self._records)

    def summarize(self, *, user_id: str | None = None) -> dict[str, Any]:
        items = [item for item in self._records if user_id is None or item.user_id == str(user_id)]
        by_kind: dict[str, dict[str, Any]] = {}
        for item in items:
            bucket = by_kind.setdefault(item.kind, {"denominator": 0, "correct": 0, "acknowledged": 0, "sources": set()})
            bucket["denominator"] += 1
            bucket["correct"] += int(item.expected == item.observed)
            bucket["acknowledged"] += int(item.acknowledged)
            bucket["sources"].add(item.source)
        for bucket in by_kind.values():
            bucket["accuracy"] = bucket["correct"] / bucket["denominator"] if bucket["denominator"] else 0.0
            bucket["acknowledgementRate"] = bucket["acknowledged"] / bucket["denominator"] if bucket["denominator"] else 0.0
            bucket["sources"] = sorted(bucket["sources"])
        return {"records": len(items), "denominator": len(items), "byKind": by_kind, "provenance": "real acknowledgements, connector receipts, or annotated sequences"}


class PrivacyGovernance:
    def __init__(self, *, identity: Any, product_database: Any, timeline: Any, user_memory_root: str | Path, outcome_ledger: OutcomeLedger | None = None) -> None:
        self.identity = identity
        self.product_database = product_database
        self.timeline = timeline
        self.user_memory_root = Path(user_memory_root)
        self.outcome_ledger = outcome_ledger
        self.evidence_root = self.user_memory_root.parent / "deletion-evidence"
        self.evidence_root.mkdir(parents=True, exist_ok=True)

    def export_account(self, user_id: str) -> dict[str, Any]:
        user_id = str(user_id)
        result: dict[str, Any] = {
            "schema": "egoassist.account-export.v1",
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "userId": user_id,
            "identity": self.identity.export_user(user_id),
            "product": self.product_database.export_user(user_id),
            "timelineMemory": self.timeline.export_user(user_id),
            "journals": [],
            "complete": True,
            "exportErrors": [],
            "evaluationOutcomes": self.outcome_ledger.records_for_user(user_id) if self.outcome_ledger else [],
        }
        journal_root = self.user_memory_root / user_id / "sessions"
        if journal_root.exists():
            for path in sorted(journal_root.glob("*.jsonl")):
                try:
                    raw = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    result["complete"] = False
                    result["exportErrors"].append({"sessionId": path.stem, "error": type(exc).__name__})
                    continue
                entries, invalid_lines = [], []
                for number, line in enumerate(raw.splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        invalid_lines.append({"line": number, "raw": line})
                if invalid_lines:
                    result["complete"] = False
                    result["exportErrors"].append({"sessionId": path.stem, "error": "invalid-journal-lines",
                                                   "lines": [item["line"] for item in invalid_lines]})
                result["journals"].append({"sessionId": path.stem, "entries": entries, "invalidLines": invalid_lines})
        legacy_root = self.user_memory_root / user_id
        for path in sorted(legacy_root.rglob("*")) if legacy_root.exists() else []:
            if not path.is_file() or path.parent == journal_root:
                continue
            try:
                if path.suffix in {".jpg", ".wav"} and "media" in path.relative_to(legacy_root).parts:
                    import base64
                    data = path.read_bytes()
                    result.setdefault("mediaFiles", []).append({"path": str(path.relative_to(legacy_root)),
                        "sha256": hashlib.sha256(data).hexdigest(), "encoding": "base64", "content": base64.b64encode(data).decode()})
                    continue
                raw = path.read_text(encoding="utf-8")
                try:
                    content: Any = json.loads(raw)
                except json.JSONDecodeError:
                    content = raw
                result.setdefault("legacyFiles", []).append({"path": str(path.relative_to(legacy_root)), "content": content})
            except (OSError, UnicodeDecodeError):
                result["complete"] = False
                result.setdefault("legacyFiles", []).append({"path": str(path.relative_to(legacy_root)), "unreadable": True})
        return result

    def delete_account(self, user_id: str) -> dict[str, Any]:
        user_id = str(user_id)
        identity_counts = self.identity.delete_user(user_id)
        product_counts = self.product_database.delete_user(user_id)
        memory_counts = self.timeline.delete_user(user_id)
        outcome_count = self.outcome_ledger.delete_user(user_id) if self.outcome_ledger else 0
        journal_count = 0
        journal_root = self.user_memory_root / user_id
        if journal_root.exists():
            for path in journal_root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    path.unlink(); journal_count += 1
                except OSError:
                    pass
        # Remove only this user's directory. Traversing the parent would erase
        # journals belonging to every other account during account deletion.
        if journal_root.exists():
            for path in sorted(journal_root.rglob("*"), reverse=True):
                if path.is_file():
                    try: path.unlink(); journal_count += 1
                    except OSError: pass
                elif path.is_dir():
                    try: path.rmdir()
                    except OSError: pass
            try: journal_root.rmdir()
            except OSError: pass
        evidence = {
            "schema": "egoassist.deletion-evidence.v1",
            "evidenceId": uuid4().hex,
            "deletedAt": datetime.now(timezone.utc).isoformat(),
            "subjectDigest": hashlib.sha256(user_id.encode("utf-8")).hexdigest(),
            "counts": {"identity": identity_counts, "product": product_counts, "memory": memory_counts, "evaluationOutcomes": outcome_count, "journals": journal_count},
        }
        (self.evidence_root / f"{evidence['evidenceId']}.json").write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
        return evidence

    def retention(self, *, user_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        from datetime import timedelta

        from egoassist.core.events import references
        from egoassist.core.journal import SessionJournal
        from egoassist.core.retention import erase_expired

        now = now or datetime.now(timezone.utc)
        policies = self.product_database.retention_policies(user_id)
        by_user = {}
        for policy in policies:
            by_user.setdefault(policy["user_id"], {})[policy["category"]] = now - timedelta(days=policy["retention_days"])
        erased_count = 0
        media_count = 0
        memory_counts = {"memoryCards": 0, "memoryDrafts": 0}
        for owner_id, cutoffs in by_user.items():
            root = self.user_memory_root / owner_id / "sessions"
            erased_sources = set()
            if "memory" in cutoffs:
                cutoff = cutoffs["memory"]
                for card in self.timeline.export_user(owner_id)["cards"]:
                    timestamp = datetime.fromisoformat(card.get("effectiveAt") or card["happenedAt"])
                    if timestamp < cutoff:
                        erased_sources.add(card["id"])
            journals = []
            for path in sorted(root.glob("*.jsonl")):
                # Ancillary diagnostic streams have no session header.
                with path.open(encoding="utf-8") as handle:
                    header = json.loads(handle.readline())
                if header.get("type") != "session":
                    continue
                journals.append(SessionJournal(path))
            previous_size = -1
            while previous_size != len(erased_sources):
                previous_size = len(erased_sources)
                for journal in journals:
                    erased = erase_expired(journal, cutoffs, source_ids=erased_sources)
                    erased_sources.update(erased["sourceIds"])
                    self.product_database.forget_turns(owner_id, erased["turnIds"])
                removed, counts = self.timeline.forget_sources(owner_id, erased_sources)
                erased_sources.update(removed)
                for name, count in counts.items():
                    memory_counts[name] += count
            erased_count += len(erased_sources)
            for journal in journals:
                rows = journal.entries()
                retained = references([row for row in rows if row.get("type") != "retention.deleted"])
                expired_media = {name for row in rows if row.get("type") == "retention.deleted" for name in row.get("mediaIds", [])}
                media_root = root / "media" / journal.session_id
                for media in media_root.glob("*"):
                    if media.is_file() and media.name in expired_media and media.stem not in retained and media.name not in retained:
                        media.unlink()
                        media_count += 1
            trace = root / "memory_trace.jsonl"
            if trace.exists():
                raw_rows = trace.read_text(encoding="utf-8").splitlines(keepends=True)
                retained = []
                cutoff = cutoffs.get("operational")
                for raw_row in raw_rows:
                    try:
                        row = json.loads(raw_row)
                    except json.JSONDecodeError:
                        retained.append(raw_row)
                        continue
                    source_ids = {str(row[key]) for key in ("card_id", "source_id", "turn_id") if row.get(key)}
                    expired = cutoff is not None and isinstance(row.get("ts"), (int, float)) and row["ts"] < cutoff.timestamp()
                    if not expired and not source_ids & erased_sources:
                        retained.append(raw_row)
                if retained != raw_rows:
                    temporary = trace.with_name(trace.name + "." + uuid4().hex + ".tmp")
                    try:
                        temporary.write_text("".join(retained), encoding="utf-8")
                        os.replace(temporary, trace)
                    finally:
                        temporary.unlink(missing_ok=True)
        result = self.product_database.apply_retention(user_id=user_id, now=now)
        result["deleted"].update(journalSources=erased_count, mediaFiles=media_count)
        for owner_id, cutoffs in by_user.items():
            if "memory" in cutoffs:
                counts = self.apply_memory_retention(owner_id, cutoffs["memory"])
                for name, count in counts.items():
                    memory_counts[name] += count
        if any(memory_counts.values()) or any("memory" in cutoffs for cutoffs in by_user.values()):
            result["deleted"]["memory"] = memory_counts
        return result

    def apply_memory_retention(self, user_id: str, cutoff: datetime) -> dict[str, int]:
        return self.timeline.apply_retention(user_id=user_id, cutoff=cutoff)

    def disclosure(self, *, ambient: bool = False) -> dict[str, Any]:
        return {"providers": provider_disclosures(ambient=ambient), "transport": transport_security_summary(), "memory": {"owner": "EgoAssist", "scopes": ["participant", "case"], "controls": ["export", "correct", "delete"]}}
