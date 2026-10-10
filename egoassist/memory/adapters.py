"""Adapters that project specialized memory producers onto timeline cards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from .store import MemoryItem
from .timeline import TimelineMemory


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        return value.item()
    return value


def import_visual_items(
    timeline: TimelineMemory,
    items: Iterable[MemoryItem],
    *,
    user_id: str,
    case_id: str,
    source: str,
    video_origin: datetime | None = None,
) -> list[dict[str, Any]]:
    """Persist visual segments without changing their native retrieval path.

    Pass ``video_origin`` (the wall-clock instant of video second 0) so each
    card carries its real interval. Without it the card can only be stamped
    with the import time, which no time-scoped query will ever match.
    """
    cards = []
    for item in items:
        caption = " ".join(str(item.caption).split()).strip()
        if not caption:
            continue
        identity = json.dumps(
            {
                "source": source,
                "item": int(item.id),
                "start": float(item.t_start),
                "end": float(item.t_end),
                "caption": caption,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        card_id = "visual-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        frame_ids = [
            str(meta.get("source_frame_index", meta.get("frame_index", ""))).strip()
            for meta in item.keyframe_meta
        ]
        source_ids = [f"{source}:memory:{item.id}"]
        source_ids.extend(f"{source}:frame:{value}" for value in frame_ids if value)
        timing: dict[str, Any] = {}
        if video_origin is not None:
            start = video_origin + timedelta(seconds=float(item.t_start))
            end = video_origin + timedelta(seconds=max(float(item.t_end), float(item.t_start) + 1e-3))
            timing = {"happened_at": start, "recorded_at": end, "effective_at": start, "effective_end": end}
        card = timeline.remember(
            caption,
            user_id=user_id,
            case_id=case_id,
            scope="case",
            kind="scene" if item.tier == "short" else "event",
            tier="long" if item.tier == "long" else "short",
            source_ids=source_ids,
            importance=0.7,
            **timing,
            metadata={
                "producer": "hierarchical-visual-memory",
                "visualEvidence": True,
                **({"timeBasis": "source-media", "videoOrigin": video_origin.isoformat()} if video_origin else {}),
                "source": source,
                "sourceMemoryId": int(item.id),
                "videoStartSeconds": float(item.t_start),
                "videoEndSeconds": float(item.t_end),
                "keyframeTimes": [float(value) for value in item.keyframe_times],
                "keyframeMeta": [_json_safe(dict(value)) for value in item.keyframe_meta],
                "childIds": [int(value) for value in item.child_ids],
            },
            supersede=False,
            card_id=card_id,
        )
        if card:
            cards.append(card)
    return cards
