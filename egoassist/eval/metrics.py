"""评测指标：定位 IoU、回答正确率、幻觉率，以及给 conformal 用的 (score, correct)。

correct 的定义（供选择性风险控制）：
  可回答查询 -> 若回答且预测概念命中 gt 概念，则 correct=True；
  不可回答查询 -> 回答即错（幻觉），correct 恒为 False（正确动作是拒答）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np

from ..data import concepts as C
from ..data.synthetic import Query
from ..qa.pipeline import QAResult, StreamingMemoryAssistant


@dataclass
class QARecord:
    query: str
    answerable: bool
    score: float
    pred_correct: bool      # 若回答，是否正确（不可回答恒 False）
    iou: float              # 定位 IoU（仅可回答且命中时有意义，否则 0）
    res: QAResult


def temporal_iou(p0: float, p1: float, g0: float, g1: float) -> float:
    if g1 < g0 or p1 < p0:
        return 0.0
    inter = max(0.0, min(p1, g1) - max(p0, g0))
    union = max(p1, g1) - min(p0, g0)
    return inter / union if union > 0 else 0.0


def _predicts_concept(res: QAResult, concept: str) -> bool:
    hay = (res.answered_text + " " + res.top_caption).lower()
    return concept in C.parse_concepts(hay)


def evaluate(asst: StreamingMemoryAssistant, queries: List[Query]) -> List[QARecord]:
    records: List[QARecord] = []
    for q in queries:
        res = asst.query(q.text)
        if q.answerable:
            correct = _predicts_concept(res, q.gt_concept)
            iou = temporal_iou(res.t0, res.t1, q.gt_t0, q.gt_t1) if correct else 0.0
        else:
            correct = False        # 回答不可回答的问题 = 幻觉
            iou = 0.0
        records.append(QARecord(q.text, q.answerable, res.score, correct, iou, res))
    return records


def summarize(records: List[QARecord], decisions) -> dict:
    """给定拒答决策，汇总覆盖率、已答风险、幻觉率、定位 IoU。"""
    answered = [(r, d) for r, d in zip(records, decisions) if d.answer]
    n = len(records)
    n_ans = len(answered)
    coverage = n_ans / n if n else 0.0
    risk = np.mean([1 - r.pred_correct for r, _ in answered]) if n_ans else 0.0

    unans = [r for r in records if not r.answerable]
    unans_answered = [r for r, d in zip(records, decisions) if (not r.answerable and d.answer)]
    halluc = len(unans_answered) / len(unans) if unans else 0.0

    hit = [r for r, d in zip(records, decisions) if d.answer and r.answerable and r.pred_correct]
    miou = np.mean([r.iou for r in hit]) if hit else 0.0
    loc_acc = np.mean([r.iou >= 0.5 for r in hit]) if hit else 0.0

    return {
        "n_queries": n, "coverage": coverage, "selective_risk": risk,
        "hallucination_rate_on_unanswerable": halluc,
        "localization_mIoU": float(miou), "localization_acc@0.5": float(loc_acc),
        "n_answered": n_ans,
    }
