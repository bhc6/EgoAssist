"""Account privacy governance and outcome evidence.

This module is deliberately an orchestration layer.  The product database,
IdentityStore, and TimelineMemory remain the owners of their own records;
privacy operations call those owners and emit only bounded deletion evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from .contracts import ProviderDisclosure


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
            data_categories=["transcript", "turn metadata"],
            purposes=["speech recognition", "dialogue response"],
            retention="provider policy; EgoAssist does not persist raw media by default",
            user_controls=["stop realtime", "delete account", "export account"],
        ),
        ProviderDisclosure(
            provider="structured-vision-model",
            data_categories=["selected video frames", "checkpoint context"],
            purposes=["hazard and progress evidence"],
            retention="frame transport only unless accepted as evidence",
            user_controls=["disable camera", "reject evidence", "delete account"],
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
                retention="frame transport only; only the derived label, time, and place are stored",
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
            "evaluationOutcomes": self.outcome_ledger.records_for_user(user_id) if self.outcome_ledger else [],
        }
        journal_root = self.user_memory_root / user_id / "sessions"
        if journal_root.exists():
            for path in sorted(journal_root.glob("*.jsonl")):
                try:
                    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                result["journals"].append({"sessionId": path.stem, "entries": entries})
        legacy_root = self.user_memory_root / user_id
        for path in sorted(legacy_root.rglob("*")) if legacy_root.exists() else []:
            if not path.is_file() or path.parent == journal_root:
                continue
            try:
                raw = path.read_text(encoding="utf-8")
                try:
                    content: Any = json.loads(raw)
                except json.JSONDecodeError:
                    content = raw
                result.setdefault("legacyFiles", []).append({"path": str(path.relative_to(legacy_root)), "content": content})
            except (OSError, UnicodeDecodeError):
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
        now = now or datetime.now(timezone.utc)
        result = self.product_database.apply_retention(user_id=user_id, now=now)
        if user_id:
            for policy in self.product_database.retention_policies(user_id):
                if policy.get("category") == "memory":
                    cutoff = now.timestamp() - int(policy["retention_days"]) * 86400
                    cutoff_time = datetime.fromtimestamp(cutoff, tz=timezone.utc)
                    result["deleted"]["memory"] = self.apply_memory_retention(user_id, cutoff_time)
        return result

    def apply_memory_retention(self, user_id: str, cutoff: datetime) -> dict[str, int]:
        return self.timeline.apply_retention(user_id=user_id, cutoff=cutoff)

    def disclosure(self, *, ambient: bool = False) -> dict[str, Any]:
        return {"providers": provider_disclosures(ambient=ambient), "transport": transport_security_summary(), "memory": {"owner": "EgoAssist", "scopes": ["participant", "case"], "controls": ["export", "correct", "delete"]}}
