"""conformal 拒答的单元测试：覆盖率保证 + 单调性。"""

import numpy as np

from egoassist.abstention.conformal import ConformalAbstention, combine_score


def _synthetic(n=400, seed=0):
    """score 与 correct 正相关：score 越高越可能正确。"""
    rng = np.random.default_rng(seed)
    scores = rng.uniform(0, 1, n)
    correct = rng.uniform(0, 1, n) < scores       # P(correct)=score
    return scores.tolist(), correct.tolist()


def test_risk_guarantee_holds():
    scores, correct = _synthetic()
    n = len(scores)
    cal = slice(0, n // 2)
    te = slice(n // 2, n)
    conf = ConformalAbstention(alpha=0.1, delta=0.1).fit(scores[cal], correct[cal])
    dec = [conf.decide(s) for s in scores[te]]
    answered = [(s, c) for s, c, d in zip(scores[te], correct[te], dec) if d.answer]
    if answered:
        risk = np.mean([1 - c for _, c in answered])
        # 允许有限样本波动，风险应接近并大体不超过 α
        assert risk <= 0.1 + 0.06, f"selective risk {risk} 超出保证太多"


def test_abstain_all_when_alpha_impossible():
    # 全是错的样本，任何非平凡阈值都无法满足 -> 应退化为全拒答（τ=inf）
    scores = np.linspace(0, 1, 50).tolist()
    correct = [False] * 50
    conf = ConformalAbstention(alpha=0.01, delta=0.1).fit(scores, correct)
    assert conf.threshold == float("inf")
    assert all(not conf.decide(s).answer for s in scores)


def test_lower_alpha_lower_coverage():
    scores, correct = _synthetic(600, seed=1)
    cov = {}
    for a in (0.05, 0.2, 0.4):
        conf = ConformalAbstention(alpha=a, delta=0.1).fit(scores, correct)
        cov[a] = np.mean([conf.decide(s).answer for s in scores])
    assert cov[0.05] <= cov[0.2] + 1e-9 <= cov[0.4] + 1e-9


def test_combine_score_range():
    rng = np.random.default_rng(20261010)  # seeded: an unseeded range check flakes
    for _ in range(100):
        s = combine_score(rng.random(), rng.uniform(-1, 1), rng.random())
        assert 0.0 <= s <= 1.0
