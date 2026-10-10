"""Group per-cue source records into event-level memory units.

One SRT cue ("我打开电脑", "Alice: 好的") is not a memory. It has no context,
competes with ~10^5 siblings for a bounded candidate scan, and loses its
interval when stored as a point. An event unit is a contiguous stretch of the
stream bounded by silence/visual gaps and a maximum span. It carries:

* a real ``[start, end]`` interval (union of member cues),
* aligned visual captions and speech transcript in one text,
* structured speakers parsed from ``Name: utterance`` transcript cues,
* every member cue's source id, so provenance and deletion still cascade.

The default 30 s span matches LightMem-Ego's short-term episode window, so the
two systems can be compared on the same unit size.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from .timeline import TimelineMemory

# "Jake: 我们走吧" / "Alice：好" / "A1_JAKE: ..." — a short label before a colon.
_SPEAKER = re.compile(r"^\s*([A-Za-z][\w .'-]{0,31}|[一-鿿]{1,6})\s*[:：]\s*(.+)$", re.S)
_FIRST_PERSON = re.compile(r"我|\bI\b|\bmy\b|\bme\b", re.I)

DEFAULT_MAX_GAP_SECONDS = 8.0
DEFAULT_MAX_SPAN_SECONDS = 30.0


def split_speaker(text: str) -> tuple[str | None, str]:
    match = _SPEAKER.match(str(text))
    if not match:
        return None, " ".join(str(text).split())
    return match.group(1).strip(), " ".join(match.group(2).split())


@dataclass
class EventUnit:
    start: float
    end: float
    captions: list[str] = field(default_factory=list)
    utterances: list[tuple[str | None, str]] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    members: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def speakers(self) -> list[str]:
        return list(dict.fromkeys(name for name, _ in self.utterances if name))

    def text(self, wearer: str | None = None) -> str:
        parts = []
        if wearer:
            # Captions are first-person narration by the wearer. Naming the
            # wearer in the stored text makes the subject source-grounded.
            parts.append(f"视角：{wearer}")
        if self.captions:
            parts.append("画面：" + "；".join(dict.fromkeys(self.captions)))
        if self.utterances:
            parts.append("对话：" + " ".join(
                f"{name}：{line}" if name else line for name, line in self.utterances
            ))
        return "\n".join(parts)

    def unit_id(self, namespace: str) -> str:
        identity = namespace + ":" + ",".join(sorted(self.source_ids))
        return "unit-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def build_event_units(
    records: Iterable[Mapping[str, Any]],
    *,
    max_gap: float = DEFAULT_MAX_GAP_SECONDS,
    max_span: float = DEFAULT_MAX_SPAN_SECONDS,
    group_key: Callable[[Mapping[str, Any]], Any] | None = None,
) -> list[EventUnit]:
    """Merge records into event units.

    Each record needs ``start``, ``end`` (seconds, same clock), ``text``,
    ``kind`` ("caption" | "transcript" | anything else treated as caption) and
    ``sourceId``. A new unit starts when the next record begins more than
    ``max_gap`` after the current unit ends, when adding it would exceed
    ``max_span``, or when ``group_key`` changes (e.g. a different video file).
    """
    if max_gap < 0 or max_span <= 0:
        raise ValueError("invalid event unit bounds")
    # Group first: records from different videos can share clock values, and
    # interleaving them would split every unit at each group change.
    ordered = sorted(records, key=lambda r: (
        str(group_key(r)) if group_key else "",
        float(r["start"]), float(r["end"]), str(r["sourceId"])))
    units: list[EventUnit] = []
    current: EventUnit | None = None
    current_group = None
    for record in ordered:
        start, end = float(record["start"]), float(record["end"])
        if end < start:
            raise ValueError("record ends before it starts")
        group = group_key(record) if group_key else None
        if (current is None or group != current_group or start - current.end > max_gap
                or max(end, current.end) - current.start > max_span):
            current = EventUnit(start=start, end=end)
            current_group = group
            units.append(current)
        current.end = max(current.end, end)
        text = str(record.get("text") or "").strip()
        if text:
            if record.get("kind") == "transcript":
                current.utterances.append(split_speaker(text))
            else:
                current.captions.append(" ".join(text.split()))
        current.source_ids.append(str(record["sourceId"]))
        current.members.append(record)
    return [unit for unit in units if unit.captions or unit.utterances]


def remember_event_unit(
    timeline: TimelineMemory,
    unit: EventUnit,
    *,
    user_id: str,
    case_id: str,
    origin: datetime,
    namespace: str,
    wearer: str | None = None,
    time_basis: str = "source-media",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Write one unit as an ``event`` card with a measured interval.

    ``wearer`` names the camera wearer. Egocentric captions are first-person
    descriptions of the wearer's own actions, so the wearer is the truthful
    subject of the unit (not a guess about a pronoun's referent).
    """
    start = origin + timedelta(seconds=unit.start)
    end = origin + timedelta(seconds=max(unit.end, unit.start + 1e-3))
    speakers = unit.speakers
    text = unit.text(wearer)
    entities = [name for name in dict.fromkeys(([wearer] if wearer else []) + speakers)
                if name and name.casefold() in text.casefold()][:12]
    evidence: dict[str, Any] = {"entities": entities, "scene": None, "times": [], "sourceText": text}
    if wearer:
        # Captions are the wearer's own narration, so "我" in them is the
        # wearer. But if another speaker in this unit says "我", that "我" is
        # *not* the wearer; claiming it would mis-attribute the statement.
        # Only then is the speaker left unresolved, and grounding flags it.
        others_first_person = any(
            name and name.casefold() != wearer.casefold() and _FIRST_PERSON.search(line)
            for name, line in unit.utterances
        )
        evidence["speaker"] = (
            {"labels": speakers, "wearer": False, "role": "mixed-speakers"}
            if others_first_person
            else {"label": wearer, "wearer": True, "role": "camera-wearer"}
        )
    return timeline.remember(
        text,
        user_id=user_id,
        case_id=case_id,
        scope="case",
        kind="event",
        happened_at=start,
        recorded_at=end,
        effective_at=start,
        effective_end=end,
        subject=wearer,
        source_ids=unit.source_ids,
        card_id=unit.unit_id(namespace),
        supersede=False,
        metadata={
            **dict(metadata or {}),
            "producer": "event-unit-builder",
            "timeBasis": time_basis,
            "unitStartSeconds": unit.start,
            "unitEndSeconds": unit.end,
            "speakers": speakers,
            "captionCount": len(unit.captions),
            "utteranceCount": len(unit.utterances),
            "evidenceContext": evidence,
        },
    )
