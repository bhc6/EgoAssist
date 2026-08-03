"""事件分段：把无界的帧流切成"微事件"（对应 LightMem-Ego 的 event segmentation）。

原则（论文一致）：由时间连续性 + 跨帧变化信号驱动，不对每帧做语义解析，
以便在流式条件下轻量运行。预留 gaze/IMU 触发钩子（G-C4：注视/IMU 是廉价、
always-on、隐私友好的分段线索，能省电又提升定位）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Iterator, List, Optional

import numpy as np

from ..config import Config
from .stream import FrameEvent


@dataclass
class Segment:
    """一个时间上连续的微事件。"""
    t_start: float
    t_end: float
    frames: List[np.ndarray] = field(default_factory=list)
    frame_times: List[float] = field(default_factory=list)
    frame_meta: List[dict] = field(default_factory=list)
    audio: List[str] = field(default_factory=list)
    audio_times: List[float] = field(default_factory=list)
    gaze: list = field(default_factory=list)
    gaze_times: List[float] = field(default_factory=list)
    wall_start: Optional[datetime] = None  # 事件开始的真实挂钟时间（带时区）
    wall_end: Optional[datetime] = None    # 事件结束的真实挂钟时间（带时区）

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start


def _change_score(a: np.ndarray, b: np.ndarray) -> float:
    """跨帧变化分数 ∈ [0,1]：用低分辨率灰度直方图的 L1 距离（廉价、无需模型）。"""
    def hist(x):
        g = x.mean(axis=2) if x.ndim == 3 else x
        h, _ = np.histogram(g, bins=32, range=(0, 255), density=True)
        return h / (h.sum() + 1e-8)
    ha, hb = hist(a), hist(b)
    return float(np.abs(ha - hb).sum() / 2.0)   # 归一化到 [0,1]


def segment_stream(stream: Iterable[FrameEvent], cfg: Config) -> Iterator[Segment]:
    """在线分段：变化超阈值、或事件过长时切边界；短于 min_len 的并入前一段。"""
    cur: Segment | None = None
    prev_frame: np.ndarray | None = None

    for ev in stream:
        boundary = False
        if cur is None:
            boundary = True
        else:
            long_enough = (ev.t - cur.t_start) >= cfg.event_max_len
            changed = prev_frame is not None and _change_score(prev_frame, ev.frame) > cfg.event_change_threshold
            # gaze/IMU 触发（预留）：注视目标跳变也可作为边界
            gaze_jump = cfg.use_gaze_imu and _gaze_jump(cur, ev)
            if (changed or gaze_jump) and cur.duration >= cfg.event_min_len:
                boundary = True
            elif long_enough:
                boundary = True

        if boundary:
            if cur is not None:
                yield cur
            cur = Segment(t_start=ev.t, t_end=ev.t,
                          wall_start=ev.wall_time, wall_end=ev.wall_time)

        cur.frames.append(ev.frame)
        cur.frame_times.append(ev.t)
        cur.frame_meta.append(dict(ev.meta))
        cur.t_end = ev.t
        # 始终更新 wall_end（取最后一帧的挂钟时间），wall_start 在切边界时已设定
        if ev.wall_time is not None:
            cur.wall_end = ev.wall_time
        if ev.audio:
            cur.audio.append(ev.audio)
            cur.audio_times.append(ev.t)
        if ev.gaze is not None:
            cur.gaze.append(ev.gaze)
            cur.gaze_times.append(ev.t)
        prev_frame = ev.frame

    if cur is not None:
        yield cur


def _gaze_jump(seg: Segment, ev: FrameEvent) -> bool:
    if ev.gaze is None or not seg.gaze:
        return False
    last = np.asarray(seg.gaze[-1], dtype=np.float32)
    now = np.asarray(ev.gaze, dtype=np.float32)
    return float(np.linalg.norm(now - last)) > 0.3
