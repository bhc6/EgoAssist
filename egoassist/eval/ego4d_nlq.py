"""Ego4D Natural Language Queries (NLQ) temporal grounding adapter.

Ego4D-NLQ is the standard benchmark for querying episodic memory:
given a video clip and a natural language query, the model must predict
the time window [start, end] where the answer is visible.

Standard metrics:
    R@1, IoU=0.3  — Recall@1 with 0.3 IoU threshold
    R@1, IoU=0.5  — Recall@1 with 0.5 IoU threshold

This adapter wraps EgoAssist's existing ``localize.py`` so the same
localisation function that runs in the QA pipeline can be evaluated
against the official NLQ annotation format.

Annotation format (nlq_val.json from the Ego4D download)::

    [
      {
        "video_uid": "...",
        "clip_uid": "...",
        "annotations": [
          {
            "language_queries": [
              {
                "query": "...",
                "clip_start_sec": 10.2,
                "clip_end_sec": 14.8
              }
            ]
          }
        ]
      }
    ]

Usage
-----
from egoassist.eval.ego4d_nlq import Ego4DNLQAdapter
from egoassist.retrieval.localize import localize

adapter = Ego4DNLQAdapter("nlq_val.json")
results = adapter.run(localize_fn=localize, n=200)
print(results["R@1_IoU0.3"], results["R@1_IoU0.5"])
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


def _iou(pred_start: float, pred_end: float, gold_start: float, gold_end: float) -> float:
    """Temporal IoU between two intervals."""
    inter_start = max(pred_start, gold_start)
    inter_end = min(pred_end, gold_end)
    intersection = max(0.0, inter_end - inter_start)
    union = max(1e-6, max(pred_end, gold_end) - min(pred_start, gold_start))
    return intersection / union


class Ego4DNLQAdapter:
    """Evaluate a temporal localisation function against Ego4D-NLQ annotations."""

    def __init__(self, annotation_path: str | Path) -> None:
        self.annotation_path = Path(annotation_path)
        self._items: list[dict[str, Any]] | None = None

    def _load(self) -> list[dict[str, Any]]:
        """Flatten nested annotation structure into a list of query items."""
        if self._items is not None:
            return self._items
        with self.annotation_path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        # Support top-level list or {"videos": [...]} wrapper
        videos = raw if isinstance(raw, list) else raw.get("videos", raw.get("clips", []))
        items = []
        for video in videos:
            video_uid = video.get("video_uid", "")
            clip_uid = video.get("clip_uid", video_uid)
            for ann in video.get("annotations", []):
                for lq in ann.get("language_queries", []):
                    query = lq.get("query", "")
                    start = lq.get("clip_start_sec")
                    end = lq.get("clip_end_sec")
                    if query and start is not None and end is not None:
                        items.append({
                            "video_uid": video_uid,
                            "clip_uid": clip_uid,
                            "query": query,
                            "gold_start": float(start),
                            "gold_end": float(end),
                        })
        self._items = items
        return self._items

    def run(
        self,
        localize_fn: Callable[[str, Any], tuple[float, float]],
        memory_or_frames: Any = None,
        *,
        n: int | None = None,
        iou_thresholds: list[float] | None = None,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Evaluate *localize_fn* against NLQ annotations.

        Args:
            localize_fn:
                ``fn(query: str, context: Any) -> (pred_start_sec, pred_end_sec)``
                The second argument is *memory_or_frames* passed through.
            memory_or_frames:
                Context passed to *localize_fn* (e.g. a HierarchicalMemory
                or a list of frames).  May be ``None`` if the function ignores it.
            n:
                Number of queries to evaluate (default: all).
            iou_thresholds:
                IoU thresholds for Recall@1.  Defaults to ``[0.3, 0.5]``.
            verbose:
                Log per-query results.

        Returns:
            dict with keys: R@1_IoU{thr} for each threshold, plus total and per-query predictions.
        """
        if iou_thresholds is None:
            iou_thresholds = [0.3, 0.5]

        items = self._load()
        if n is not None:
            items = items[:n]

        hits = {thr: 0 for thr in iou_thresholds}
        predictions: list[dict[str, Any]] = []

        for idx, item in enumerate(items):
            try:
                pred_start, pred_end = localize_fn(item["query"], memory_or_frames)
            except Exception as exc:
                LOGGER.warning("Ego4D-NLQ localize_fn failed on item %d: %s", idx, exc)
                pred_start, pred_end = 0.0, 0.0

            score = _iou(pred_start, pred_end, item["gold_start"], item["gold_end"])
            entry = {
                "clip_uid": item["clip_uid"],
                "query": item["query"],
                "pred": [pred_start, pred_end],
                "gold": [item["gold_start"], item["gold_end"]],
                "iou": round(score, 4),
            }
            for thr in iou_thresholds:
                entry[f"hit@{thr}"] = score >= thr
                if score >= thr:
                    hits[thr] += 1
            predictions.append(entry)

            if verbose:
                LOGGER.info(
                    "[%d/%d] IoU=%.3f pred=[%.1f,%.1f] gold=[%.1f,%.1f] query=%s",
                    idx + 1, len(items), score,
                    pred_start, pred_end, item["gold_start"], item["gold_end"],
                    item["query"][:60],
                )

        total = len(items)
        result: dict[str, Any] = {
            "benchmark": "Ego4D-NLQ",
            "total": total,
            "predictions": predictions,
        }
        for thr in iou_thresholds:
            key = f"R@1_IoU{thr}"
            result[key] = round(hits[thr] / total, 4) if total else 0.0

        return result
