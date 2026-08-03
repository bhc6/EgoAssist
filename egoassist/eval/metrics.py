"""评测指标：定位 IoU、回答正确率、幻觉率，以及给 conformal 用的 (score, correct)。

correct 的定义（供选择性风险控制）：
  可回答查询 -> 若回答且预测概念命中 gt 概念，则 correct=True；
  不可回答查询 -> 回答即错（幻觉），correct 恒为 False（正确动作是拒答）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from ..data import concepts as C
from ..data.synthetic import Query
from ..qa.pipeline import QAResult, StreamingMemoryAssistant
from .real_metrics import temporal_iou


@dataclass
class QARecord:
    query: str
    answerable: bool
    score: float
    pred_correct: bool      # 若回答，是否正确（不可回答恒 False）
    iou: float              # 定位 IoU（仅可回答且命中时有意义，否则 0）
    res: QAResult


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


