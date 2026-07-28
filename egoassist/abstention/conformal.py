"""校准拒答（贡献点二、核心新意）。

把"答 / 不答"变成一个**有覆盖率保证**的决策：只有当置信分数 s(x) ≥ τ 时才回答，
否则拒答或请求澄清，而不是幻觉。阈值 τ 用**校准集**标定，使"已回答样本上的
风险（错误率）"以高置信度不超过目标 α——这是 selective prediction / conformal
risk control 的标准框架，直接回应 SuperMemory-VQA / MemLens 指认的
"前沿系统宁可幻觉也不说证据不足"。

置信分数 s(x) 融合三路信号：VLM 自评置信度、检索相似度、定位锐度。
不可回答查询若被回答，一律记为错误（幻觉），因此该框架同时控制幻觉率。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

import numpy as np


@dataclass
class Decision:
    answer: bool          # True=回答，False=拒答
    score: float
    threshold: float


class ConformalAbstention:
    """分布无关的选择性风险控制。

    fit() 传入校准集 (scores, correct)；correct_i=True 表示"若回答则正确"
    （不可回答样本恒为 False）。calibrate() 选出满足风险上界的最小阈值（即最大覆盖率）。
    """

    def __init__(self, alpha: float = 0.1, delta: float = 0.1, mode: str = "marginal"):
        """mode:
        - "marginal"：选满足经验选择性风险 ≤ α 的**最大覆盖率**阈值（风险≈α，
          适合小校准集与可视化，给出边际（in-expectation）保证）。
        - "hoeffding"：要求风险的 Hoeffding 上置信界 ≤ α（高概率 1-δ 保证，
          更保守，需要较大校准集，否则会退化为全拒答）。
        """
        assert mode in ("marginal", "hoeffding")
        self.alpha = alpha        # 目标风险上界
        self.delta = delta        # 置信参数（hoeffding 模式下以 1-δ 成立）
        self.mode = mode
        self.threshold: float = math.inf
        self._fitted = False

    def fit(self, scores: Sequence[float], correct: Sequence[bool]) -> "ConformalAbstention":
        self.cal_scores = np.asarray(scores, dtype=np.float64)
        self.cal_loss = 1.0 - np.asarray(correct, dtype=np.float64)   # L_i ∈ {0,1}
        self.threshold = self._calibrate()
        self._fitted = True
        return self

    def _risk_estimate(self, loss: np.ndarray) -> float:
        n = len(loss)
        if n == 0:
            return math.inf          # 没样本无法保证 -> 视作不满足
        emp = float(loss.mean())
        if self.mode == "hoeffding":
            return emp + math.sqrt(math.log(1.0 / self.delta) / (2.0 * n))
        # marginal：加 (n+1) 有限样本修正（保守一点，仍近似 α）
        return (n * emp + 1.0) / (n + 1.0)

    def _calibrate(self) -> float:
        """在所有候选阈值里，选风险≤α 且**覆盖率最大**的 τ；都不满足则全拒答。"""
        candidates = np.unique(self.cal_scores)
        best_tau, best_cov = math.inf, -1.0      # 默认全拒答（风险=0，平凡满足）
        for tau in candidates:
            answered = self.cal_scores >= tau
            cov = float(answered.mean())
            if answered.sum() == 0:
                continue
            if self._risk_estimate(self.cal_loss[answered]) <= self.alpha and cov > best_cov:
                best_tau, best_cov = float(tau), cov
        return best_tau

    def decide(self, score: float) -> Decision:
        assert self._fitted, "先 fit() 校准"
        return Decision(answer=(score >= self.threshold), score=score,
                        threshold=self.threshold)

    # ---- 评测：risk–coverage 曲线 ----
    def risk_coverage_curve(self, scores: Sequence[float],
                            correct: Sequence[bool]) -> List[Tuple[float, float, float]]:
        """返回 [(threshold, coverage, risk)]，按覆盖率从高到低。"""
        s = np.asarray(scores, dtype=np.float64)
        loss = 1.0 - np.asarray(correct, dtype=np.float64)
        out = []
        for tau in sorted(np.unique(s)):
            ans = s >= tau
            cov = float(ans.mean())
            risk = float(loss[ans].mean()) if ans.sum() else 0.0
            out.append((float(tau), cov, risk))
        out.sort(key=lambda x: x[1], reverse=True)
        return out


def combine_score(vlm_conf: float, retrieval_sim: float, localize_conf: float,
                  w: Tuple[float, float, float] = (0.5, 0.3, 0.2)) -> float:
    """把三路信号融合成单一置信分数 s(x) ∈ [0,1]。权重可做消融。"""
    sim01 = 0.5 * (retrieval_sim + 1.0)          # 余弦 [-1,1] -> [0,1]
    s = w[0] * vlm_conf + w[1] * sim01 + w[2] * localize_conf
    return float(np.clip(s, 0.0, 1.0))
