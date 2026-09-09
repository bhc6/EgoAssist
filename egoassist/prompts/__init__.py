"""Versioned, inspectable prompt assets shared by runtime and evaluation."""

import hashlib
import json
from pathlib import Path
from functools import lru_cache

ROOT = Path(__file__).parent


@lru_cache(maxsize=64)
def _read(name: str, modified: int) -> dict:
    return json.loads((ROOT / (name + ".json")).read_text(encoding="utf-8"))


def prompt_text(key: str, **values) -> str:
    bundle, entry = key.split("/", 1)
    path = ROOT / (bundle + ".json")
    text = _read(bundle, path.stat().st_mtime_ns)[entry]
    return text.format(**values) if values else text


def prompt_bundle(name: str) -> dict:
    path = ROOT / (name + ".json")
    return dict(_read(name, path.stat().st_mtime_ns))


def prompt_catalog() -> list[dict]:
    result = []
    for path in sorted(ROOT.glob("*.json")):
        raw = path.read_bytes()
        result.append({"path": "egoassist/prompts/" + path.name,
                       "sha256": hashlib.sha256(raw).hexdigest(),
                       "templates": json.loads(raw)})
    return result


def compact_audit_payload(kind: str, payload: dict) -> dict:
    """Keep polling light; full requests remain in the owner-scoped journal API."""
    value = dict(payload)
    if kind == "control_decision" and isinstance(value.get("decision"), dict):
        decision = dict(value["decision"])
        metrics = dict(decision.get("requestMetrics") or {})
        if "messages" in metrics:
            metrics.pop("messages")
            metrics["messagesAvailable"] = True
            decision["requestMetrics"] = metrics
            value["decision"] = decision
    fields = {
        "model.prompt": ("instructions", "tools"),
        "response.dispatched": ("instructions",),
        "insight.request": ("messages",),
        "insight.result": ("sources", "rawOutput"),
        "answer.job": ("instructions", "context"),
        "answer.request": ("messages",),
    }.get(kind, ())
    for field in fields:
        if field in value:
            content = value.pop(field)
            value[field + "Length"] = len(content) if isinstance(content, (str, list, dict)) else None
    if fields or kind == "control_decision":
        value["fullAuditEndpoint"] = "/api/prompts?session=<current-session>"
    return value
