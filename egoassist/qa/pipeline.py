"""端到端 pipeline：把采集→记忆→检索→定位→回答→校准拒答串起来。

用法：
    asst = StreamingMemoryAssistant(backend, cfg)
    asst.ingest(stream)                 # 在线构建分层记忆
    res = asst.query("where are my keys?")   # 得到带证据窗口+置信分数的原始结果
    asst.calibrate(cal_scores, cal_correct)  # 用校准集标定拒答阈值
    final = asst.answer_or_abstain("...")    # 应用校准拒答
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..abstention.conformal import ConformalAbstention, Decision, combine_score
from ..backends.base import VLMBackend
from ..capture.events import segment_stream
from ..capture.stream import FrameStream
from ..config import Config
from ..memory.store import HierarchicalMemory
from ..retrieval.localize import Localization, localize
from ..retrieval.router import route_and_retrieve


@dataclass
class QAResult:
    query: str
    answered_text: str
    t0: float
    t1: float
    score: float                 # 融合置信分数 s(x)
    vlm_conf: float
    retrieval_sim: float
    localize_conf: float
    top_caption: str
    n_candidates: int
    top_memory_id: int
    candidate_ids: List[int]
    evidence_frame_times: List[float]


class StreamingMemoryAssistant:
    def __init__(self, backend: VLMBackend, cfg: Optional[Config] = None):
        self.backend = backend
        self.cfg = cfg or Config()
        self.memory = HierarchicalMemory(backend, self.cfg)
        self.abstention = ConformalAbstention(alpha=self.cfg.risk_alpha)
        self._calibrated = False

    # ---- 1) 在线摄入 ----
    def ingest(self, stream: FrameStream) -> int:
        n = 0
        for seg in segment_stream(stream, self.cfg):
            self.memory.ingest(seg)
            n += 1
        return n

    # ---- 2) 单查询（不做拒答，返回原始信号）----
    def query(self, q: str) -> QAResult:
        cands = route_and_retrieve(q, self.memory, self.backend, self.cfg)
        if not cands:
            return QAResult(
                q, "I don't have any relevant memory.", 0, 0, 0.0, 0.0,
                -1.0, 0.0, "", 0, -1, [], [],
            )
        top = cands[0]
        loc: Localization = localize(q, top.item, self.backend, self.cfg)
        ans = self.backend.answer(q, [c.item.caption for c in cands], loc.frames)
        score = combine_score(ans.confidence, top.score, loc.confidence)
        return QAResult(
            query=q, answered_text=ans.text, t0=loc.t0, t1=loc.t1, score=score,
            vlm_conf=ans.confidence, retrieval_sim=top.score, localize_conf=loc.confidence,
            top_caption=top.item.caption, n_candidates=len(cands),
            top_memory_id=top.item.id,
            candidate_ids=[candidate.item.id for candidate in cands],
            evidence_frame_times=loc.frame_times)

    # ---- 3) 校准拒答阈值 ----
    def calibrate(self, scores: List[float], correct: List[bool]):
        self.abstention.fit(scores, correct)
        self._calibrated = True

    # ---- 4) 带拒答的最终回答 ----
    def answer_or_abstain(self, q: str) -> tuple[QAResult, Decision]:
        res = self.query(q)
        assert self._calibrated, "先 calibrate() 标定阈值"
        dec = self.abstention.decide(res.score)
        if not dec.answer:
            res = QAResult(res.query, "证据不足，我不确定（abstain）。", res.t0, res.t1,
                           res.score, res.vlm_conf, res.retrieval_sim, res.localize_conf,
                           res.top_caption, res.n_candidates, res.top_memory_id,
                           res.candidate_ids, res.evidence_frame_times)
        return res, dec
