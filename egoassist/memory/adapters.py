"""Adapters that project specialized memory producers onto timeline cards."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .interface import MemoryBackend  # noqa: F401 – used in docstrings/subclassers
    from .store import HierarchicalMemory

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
) -> list[dict[str, Any]]:
    """Persist visual segments without changing their native retrieval path."""
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
        card = timeline.remember(
            caption,
            user_id=user_id,
            case_id=case_id,
            scope="case",
            kind="scene" if item.level == "short" else "event",
            tier="long" if item.level == "long" else "short",
            source_ids=source_ids,
            importance=0.7,
            metadata={
                "producer": "hierarchical-visual-memory",
                "visualEvidence": True,
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


# ---------------------------------------------------------------------------
# Protocol adapters for MemoryBackend
# ---------------------------------------------------------------------------

class TimelineMemoryAdapter:
    """MemoryBackend-compatible wrapper around TimelineMemory."""

    def __init__(self, timeline: TimelineMemory, *, user_id: str, case_id: str | None = None, scope: str = "participant"):
        self._timeline = timeline
        self._user_id = user_id
        self._case_id = case_id
        self._scope = scope

    def ingest_text(self, text: str, *, user_id: str, kind: str, metadata: dict[str, Any]) -> str:
        card = self._timeline.remember(
            text,
            user_id=user_id,
            case_id=self._case_id,
            scope=self._scope,
            kind=kind,
            tier="short",
            metadata=metadata,
        )
        return card["id"] if card else ""

    def retrieve_text(self, query: str, *, user_id: str, limit: int = 8) -> list[dict[str, Any]]:
        result = self._timeline.retrieve(
            query,
            user_id=user_id,
            case_id=self._case_id,
            scope=self._scope,
            limit=limit,
        )
        selected = result.get("selected", [])
        return [
            {
                "id": item["cardId"],
                "text": item["card"]["text"],
                "score": item["score"],
                **item["card"],
            }
            for item in selected
        ]


class HierarchicalMemoryAdapter:
    """MemoryBackend-compatible wrapper around HierarchicalMemory.

    Note: HierarchicalMemory uses Segment-based ingest, so this adapter
    converts text → synthetic Segment for compatibility.
    """

    def __init__(self, hierarchical: HierarchicalMemory):
        self._hierarchical = hierarchical

    def ingest_text(self, text: str, *, user_id: str, kind: str, metadata: dict[str, Any]) -> str:
        # Synthetic segment: no frames, just text as caption
        from ..capture.events import Segment
        import numpy as np
        seg = Segment(
            t_start=0.0,
            t_end=0.0,
            frames=[],
            frame_times=[],
            frame_meta=[],
            audio=[text],
            audio_times=[0.0],
        )
        item = self._hierarchical.ingest(seg)
        return str(item.id)

    def retrieve_text(self, query: str, *, user_id: str, limit: int = 8) -> list[dict[str, Any]]:
        from ..retrieval.router import route_and_retrieve
        from ..config import Config
        candidates = route_and_retrieve(query, self._hierarchical, self._hierarchical.backend, Config())
        return [
            {
                "id": str(cand.item.id),
                "text": cand.item.caption,
                "score": cand.score,
            }
            for cand in candidates[:limit]
        ]
