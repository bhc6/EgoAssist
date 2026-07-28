"""精确时间定位（贡献点一）。

在候选事件内部做细粒度定位，输出**证据窗口** [t0,t1] 与**定位置信度**。
这正面攻击 S-EMBER 指认的"定位悖论"——定位精度不随模型增大改善，
因此我们把定位当作候选窗内的显式打分+校准问题，而非寄望更大 backbone。

定位置信度用 top-1 与 top-2 窗口的 softmax margin 表示（越尖锐越可信），
它同时作为下游校准拒答的一个信号。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np

from ..backends.base import VLMBackend
from ..config import Config
from ..memory.store import MemoryItem


@dataclass
class Localization:
    t0: float
    t1: float
    confidence: float           # [0,1] 定位置信度（窗口锐度）
    frames: List[np.ndarray]    # 证据帧
    frame_times: List[float]    # 每张证据帧的视频内真实相对时间
    caption_hint: str


def localize(query: str, item: MemoryItem, backend: VLMBackend, cfg: Config) -> Localization:
    q = backend.embed_text(query)
    kfs = item.keyframes
    if len(kfs) == 0 or not np.any(q):
        return Localization(
            item.t_start, item.t_end, 0.0, kfs,
            list(item.keyframe_times), item.caption,
        )

    if len(item.keyframe_times) != len(kfs):
        raise ValueError("keyframes and keyframe_times must have the same length")
    ts = np.asarray(item.keyframe_times, dtype=np.float64)
    win = max(cfg.localize_win, (item.t_end - item.t_start) / max(1, len(kfs)))

    # 滑窗打分
    windows = []
    t = item.t_start
    while t <= max(item.t_start, item.t_end - 1e-6):
        t_hi = min(item.t_end, t + win)
        sel = [f for f, tt in zip(kfs, ts) if t <= tt <= t_hi]
        if sel:
            score = float(np.dot(q, backend.embed_frames(sel)))
            sel_times = [float(tt) for tt in ts if t <= tt <= t_hi]
            windows.append((t, t_hi, score, sel, sel_times))
        t += cfg.localize_stride
    if not windows:
        return Localization(
            item.t_start, item.t_end, 0.0, kfs,
            list(item.keyframe_times), item.caption,
        )

    scores = np.array([w[2] for w in windows], dtype=np.float32)
    best = int(np.argmax(scores))
    conf = _softmax_margin(scores)
    t0, t1, _, sel, sel_times = windows[best]
    return Localization(
        t0=t0, t1=t1, confidence=conf, frames=sel,
        frame_times=sel_times, caption_hint=item.caption,
    )


def _softmax_margin(scores: np.ndarray, temp: float = 5.0) -> float:
    """定位锐度：softmax 后 top-1 概率，∈ (0,1]。窗口越少越接近 1。"""
    if len(scores) == 1:
        return 1.0
    p = np.exp(temp * (scores - scores.max()))
    p = p / p.sum()
    return float(p.max())
