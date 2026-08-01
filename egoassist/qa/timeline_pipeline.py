"""EgoLife benchmark pipeline backed by TimelineMemory (the new memory system).

Drop-in replacement for StreamingMemoryAssistant that stores VLM captions as
timeline cards (kind="scene") and retrieves via BM25 + semantic text search,
without retaining raw frame pixels after ingest.

Trade-off vs. HierarchicalMemory path:
  - temporal_iou granularity = segment boundary (≈ event_max_len seconds)
    rather than sub-second sliding-window localization.
  - Retrieval is text-driven (timeline hybrid), not frame-embedding cosine.
  - Faster per-question inference (no GPU call during retrieval).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..abstention.conformal import ConformalAbstention, Decision, combine_score
from ..backends.base import VLMBackend
from ..capture.events import segment_stream
from ..capture.stream import FrameStream
from ..config import Config
from ..memory.timeline import TimelineMemory
from .pipeline import QAResult  # reuse the shared result dataclass

# scene + event are the kinds stored during ingest; exclude them from default
# retrieval elsewhere but include them here.
_BENCHMARK_KINDS = frozenset({"scene", "event"})
# Map timeline scores [0, 1] → cosine-compatible range for combine_score
_COMBINE_WEIGHTS = (0.6, 0.3, 0.1)


class TimelineAssistant:
    """StreamingMemoryAssistant-compatible wrapper over TimelineMemory."""

    def __init__(
        self,
        backend: VLMBackend,
        cfg: Optional[Config] = None,
        *,
        timeline: TimelineMemory,
        user_id: str,
        case_id: str,
    ) -> None:
        self.backend = backend
        self.cfg = cfg or Config()
        self._timeline = timeline
        self._user_id = user_id
        self._case_id = case_id
        self.abstention = ConformalAbstention(alpha=self.cfg.risk_alpha)
        self._calibrated = False
        # Track card IDs written during ingest for provenance checks.
        self._ingested_card_ids: list[str] = []

    # ------------------------------------------------------------------
    # 1) Ingest
    # ------------------------------------------------------------------

    def ingest(self, stream: FrameStream) -> int:
        """Caption video segments and store them as scene cards in the timeline."""
        n = 0
        for seg in segment_stream(stream, self.cfg):
            cap = self.backend.caption(seg.frames)
            cap = " ".join(cap.split()).strip()
            if not cap:
                cap = "[no visual content]"
            kf_times = [float(t) for t in seg.frame_times]
            kf_meta = [dict(m) for m in seg.frame_meta]
            card = self._timeline.remember(
                cap,
                user_id=self._user_id,
                case_id=self._case_id,
                scope="case",
                kind="scene",
                tier="short",
                importance=0.7,
                metadata={
                    "producer": "timeline-visual-pipeline",
                    "videoStartSeconds": float(seg.t_start),
                    "videoEndSeconds": float(seg.t_end),
                    "keyframeTimes": kf_times,
                    "keyframeMeta": kf_meta,
                },
            )
            if card:
                self._ingested_card_ids.append(card["id"])
            n += 1
        return n

    # ------------------------------------------------------------------
    # 2) Single query (raw scores, no abstention)
    # ------------------------------------------------------------------

    def query(self, q: str) -> QAResult:
        result = self._timeline.retrieve(
            q,
            user_id=self._user_id,
            case_id=self._case_id,
            scope="case",
            kinds=_BENCHMARK_KINDS,
            limit=self.cfg.retrieve_topk,
        )
        selected = result.get("selected", [])
        if not selected:
            return QAResult(
                q, "I don't have any relevant memory.",
                0.0, 0.0, 0.0, 0.0, -1.0, 0.0, "", 0, "", [], [],
            )
        top = selected[0]
        top_card = top["card"]
        top_meta = top_card.get("metadata") or {}
        t0 = float(top_meta.get("videoStartSeconds", 0.0))
        t1 = float(top_meta.get("videoEndSeconds", t0))
        kf_times: List[float] = top_meta.get("keyframeTimes") or []
        captions = [c["card"]["text"] for c in selected]
        candidate_ids = [c["cardId"] for c in selected]
        retrieval_sim = float(top.get("score", 0.0))
        ans = self.backend.answer(q, captions, [])
        # Timeline scores are in [0, 1]; map to [-1, 1] for combine_score's
        # cosine normalisation step (sim01 = 0.5 * (sim + 1)).
        sim_as_cosine = 2.0 * retrieval_sim - 1.0
        score = combine_score(
            ans.confidence, sim_as_cosine, 1.0, w=_COMBINE_WEIGHTS,
        )
        return QAResult(
            query=q,
            answered_text=ans.text,
            t0=t0,
            t1=t1,
            score=score,
            vlm_conf=ans.confidence,
            retrieval_sim=retrieval_sim,
            localize_conf=1.0,
            top_caption=top_card.get("text", ""),
            n_candidates=len(selected),
            top_memory_id=top["cardId"],
            candidate_ids=candidate_ids,
            evidence_frame_times=kf_times,
        )

    # ------------------------------------------------------------------
    # 3) Calibration
    # ------------------------------------------------------------------

    def calibrate(self, scores: List[float], correct: List[bool]) -> None:
        self.abstention.fit(scores, correct)
        self._calibrated = True

    # ------------------------------------------------------------------
    # 4) Answer with abstention
    # ------------------------------------------------------------------

    def answer_or_abstain(self, q: str) -> tuple[QAResult, Decision]:
        res = self.query(q)
        assert self._calibrated, "先 calibrate() 标定阈值"
        dec = self.abstention.decide(res.score)
        if not dec.answer:
            res = QAResult(
                res.query, "证据不足，我不确定（abstain）。",
                res.t0, res.t1, res.score, res.vlm_conf, res.retrieval_sim,
                res.localize_conf, res.top_caption, res.n_candidates,
                res.top_memory_id, res.candidate_ids, res.evidence_frame_times,
            )
        return res, dec

    # ------------------------------------------------------------------
    # Snapshot helpers (replaces save_memory / load_memory for this path)
    # ------------------------------------------------------------------

    def all_captions(self) -> list[str]:
        """Return captions of all ingested cards (for invalid-caption detection)."""
        return [
            c["card"]["text"]
            for c in self._timeline.retrieve(
                "",
                user_id=self._user_id,
                case_id=self._case_id,
                scope="case",
                kinds=_BENCHMARK_KINDS,
                limit=9999,
            ).get("candidates", [])
        ]

    def save_manifest(self, path: Path, provenance: dict) -> Path:
        """Write a lightweight manifest so the next run can skip re-ingest."""
        manifest = {
            **provenance,
            "engine": "timeline-memory",
            "cardCount": len(self._ingested_card_ids),
            "cardIds": self._ingested_card_ids,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_if_cached(self, path: Path, expected_provenance: dict) -> int | None:
        """Return ingested card count if a valid cached manifest exists, else None."""
        if not path.exists():
            return None
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        # Verify the provenance keys we care about
        for key in ("runFingerprint", "videoId", "videoSha256", "sampleFps"):
            if stored.get(key) != expected_provenance.get(key):
                return None
        if stored.get("engine") != "timeline-memory":
            return None
        self._ingested_card_ids = list(stored.get("cardIds", []))
        return int(stored.get("cardCount", 0))
