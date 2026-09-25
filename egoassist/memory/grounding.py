"""Shared evidence contract for live writes, recovery and legacy read projections.

Original statements are immutable evidence, not assertions about the current turn.
This module deliberately leaves unsupported language unresolved instead of guessing.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .temporal import ClockContext, resolve_time_semantics


_REFERENCE = re.compile(r"^(?:他|她|它|他们|她们|它们|这个|那个|这里|那里|这儿|那儿|"
                        r"这件事|那件事|对方|he|she|it|they|this|that|here|there)(?:的|\b|$)", re.I)
_DEICTIC = re.compile(r"这个|那个|这里|那里|这儿|那儿|这件事|那件事|(?<!其)[他她它](?:们)?|"
                      r"\b(?:he|she|it|they|here|there)\b", re.I)


def evidence_contract(card: dict[str, Any]) -> dict[str, Any]:
    """Derive temporal/identity boundaries using the *source* clock, never recall time."""
    metadata = card.get("metadata") or {}
    evidence = metadata.get("evidenceContext") or {}
    source_clock = evidence.get("clock") or {}
    anchor = source_clock.get("nowUtc") or card.get("recordedAt")
    clock = ClockContext.current(
        source_clock.get("timezone") or card.get("timezone") or "UTC",
        source_clock.get("utcOffsetMinutes", card.get("utcOffsetMinutes", 0)),
        now_utc=datetime.fromisoformat(anchor.replace("Z", "+00:00")),
    )
    text = str(card.get("text") or "")
    source = str(evidence.get("sourceText") or text)
    # A supported, literal excerpt can carry its own date. A paraphrase cannot
    # discard temporal qualifications from the actual source statement.
    excerpt = resolve_time_semantics(text, clock, kind=None)
    temporal_text = text if text in source and excerpt["expressions"] else source
    temporal = resolve_time_semantics(temporal_text, clock, kind=None)
    start, end = card.get("effectiveAt"), card.get("effectiveEnd")
    status = "resolved" if start else "unspecified"
    # Do not turn a video import's wall clock into the video's calendar date.
    imported = "videoStartSeconds" in metadata or metadata.get("visualEvidence")
    if not imported:
        if temporal["status"] == "needs-clarification":
            start = end = None
            status = "needs-clarification"
        elif temporal["dateExplicit"] or (temporal["clockExplicit"] and not start):
            start, end = temporal["effectiveAt"], temporal["effectiveEnd"]
            status = "recurring" if temporal["recurrence"] else "resolved"
        elif temporal["recurrence"]:
            start = end = None
            status = "recurring"
    subject = str(card.get("subject") or "").strip()
    value = str(card.get("value") or "").strip()
    reference_unknown = bool(_REFERENCE.search(subject) or _REFERENCE.search(value))
    reference_unknown = reference_unknown or bool(not subject and _DEICTIC.search(source))
    if subject and _DEICTIC.search(source) and subject.casefold() not in source.casefold():
        reference_unknown = True
    speaker = evidence.get("speaker")
    if isinstance(speaker, dict) and speaker.get("wearer") is not True and re.search(r"我|\bI\b|\bmy\b", source):
        reference_unknown = True
    return {
        "version": 1,
        "statementAt": clock.now_utc.isoformat(timespec="milliseconds"),
        "statementLocalTime": clock.local_now.isoformat(timespec="seconds"),
        "timezone": clock.timezone_name,
        "effectiveAt": start,
        "effectiveEnd": end,
        "temporalStatus": status,
        "timeExpressions": temporal["expressions"],
        "referenceStatus": "needs-clarification" if reference_unknown else "source-scoped",
        "recurrence": temporal["recurrence"] or card.get("recurrence"),
        "claimBoundary": (
            "Historical source statement. Relative dates refer to statementLocalTime, never the recall date. "
            "First person refers to the source speaker, not automatically the account owner. "
            "An occurrence, plan or routine does not prove completion now. Missing evidence does not prove non-occurrence."
        ),
    }


def project_card(card: dict[str, Any]) -> dict[str, Any]:
    contract = evidence_contract(card)
    card["grounding"] = contract
    card["effectiveAt"] = contract["effectiveAt"]
    card["effectiveEnd"] = contract["effectiveEnd"]
    card["recurrence"] = contract["recurrence"]
    return card


HISTORY_STATEMENT_POLICY = (
    "Each replayed message is a historical statement labeled with its original recordedAt. "
    "Relative dates and pronouns belong to that source turn, not the current turn. "
    "If its original local date or referent cannot be determined, keep it unknown."
)


def dated_statement(text: str, timestamp: str | None, *, compact: bool = False) -> str:
    if compact:
        # Caller must supply HISTORY_STATEMENT_POLICY once as system context.
        return f"[recordedAt={timestamp or 'unknown time'}]\n" + text
    return (f"[Historical statement recorded at {timestamp or 'unknown time'}; "
            "relative dates and pronouns belong to this source turn, not the current turn. "
            "If its original local date or referent cannot be determined, keep it unknown.]\n" + text)
