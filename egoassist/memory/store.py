"""分层记忆：current / short-term / long-term，带时间戳锚点。

对齐 LightMem-Ego 的三级记忆与共享时间线；写入即在线（online），
整理（consolidation）异步进行。每条记忆保留少量关键帧，供后续精确时间定位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from ..backends.base import VLMBackend
from ..capture.events import Segment
from ..config import Config


@dataclass
class MemoryItem:
    id: int
    t_start: float
    t_end: float
    embedding: np.ndarray
    caption: str
    keyframes: List[np.ndarray] = field(default_factory=list)  # 用于时间定位
    keyframe_times: List[float] = field(default_factory=list)  # 视频内真实相对时间（秒）
    keyframe_meta: List[dict] = field(default_factory=list)
    audio_events: List[dict] = field(default_factory=list)
    level: str = "short"                                       # short / long
    child_ids: List[int] = field(default_factory=list)         # long 聚合的 short id

    @property
    def t_center(self) -> float:
        return 0.5 * (self.t_start + self.t_end)


class HierarchicalMemory:
    def __init__(self, backend: VLMBackend, cfg: Config):
        self.backend = backend
        self.cfg = cfg
        self._next_id = 0
        self.current: List[MemoryItem] = []
        self.short_term: List[MemoryItem] = []
        self.long_term: List[MemoryItem] = []
        # 完整来源注册表；活动检索仍只使用 current/short_term/long_term。
        self._items_by_id: dict[int, MemoryItem] = {}
        self._since_consolidate = 0

    # ---- 在线写入 ----
    def ingest(self, seg: Segment) -> MemoryItem:
        emb = self.backend.embed_frames(seg.frames)
        cap = self.backend.caption(seg.frames)
        if seg.audio:
            cap = cap + " | heard: " + " ".join(seg.audio)
        # 保留至多 8 个关键帧用于定位（均匀采样）
        kf, kf_times, kf_meta = _subsample_timed(
            seg.frames, seg.frame_times, seg.frame_meta, 8
        )
        audio_events = [
            {"source_time_sec": float(t), "text": text}
            for t, text in zip(seg.audio_times, seg.audio)
        ]
        item = MemoryItem(id=self._new_id(), t_start=seg.t_start, t_end=seg.t_end,
                          embedding=emb, caption=cap, keyframes=kf,
                          keyframe_times=kf_times, keyframe_meta=kf_meta,
                          audio_events=audio_events, level="short")
        self._items_by_id[item.id] = item
        self.short_term.append(item)
        self._update_current(item)
        self._since_consolidate += 1
        if (self._since_consolidate >= self.cfg.consolidate_every
                or len(self.short_term) > self.cfg.short_term_capacity):
            self.consolidate()
        return item

    def _update_current(self, item: MemoryItem):
        self.current.append(item)
        cutoff = item.t_end - self.cfg.current_window
        self.current = [m for m in self.current if m.t_end >= cutoff]

    # ---- 异步整理：把最旧的 short-term 合并进 long-term ----
    def consolidate(self):
        self._since_consolidate = 0
        if len(self.short_term) <= self.cfg.short_term_capacity // 2:
            return
        n_move = len(self.short_term) - self.cfg.short_term_capacity // 2
        movers = self.short_term[:n_move]
        self.short_term = self.short_term[n_move:]
        # 简单策略：按概念/caption 聚类合并成 episode（此处按相邻相似度贪心合并）
        for group in _greedy_group(movers):
            merged = self._merge(group)
            self.long_term.append(merged)
            self._items_by_id[merged.id] = merged

    def _merge(self, group: List[MemoryItem]) -> MemoryItem:
        emb = np.mean([m.embedding for m in group], axis=0)
        emb = emb / (np.linalg.norm(emb) + 1e-8)
        caps = list(dict.fromkeys(m.caption for m in group))
        timed_frames = sorted(
            (
                (t, f, meta)
                for m in group
                for t, f, meta in zip(
                    m.keyframe_times, m.keyframes, m.keyframe_meta
                )
            ),
            key=lambda pair: pair[0],
        )
        kf, kf_times, kf_meta = _subsample_timed(
            [frame for _, frame, _ in timed_frames],
            [timestamp for timestamp, _, _ in timed_frames],
            [meta for _, _, meta in timed_frames],
            8,
        )
        return MemoryItem(
            id=self._new_id(), t_start=group[0].t_start, t_end=group[-1].t_end,
            embedding=emb.astype(np.float32), caption="; ".join(caps[:4]),
            keyframes=kf, keyframe_times=kf_times, keyframe_meta=kf_meta,
            audio_events=[event for m in group for event in m.audio_events],
            level="long",
            child_ids=[m.id for m in group])

    def all_items(self) -> List[MemoryItem]:
        return self.short_term + self.long_term

    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i


# ---- helpers ----
def _subsample(frames: List[np.ndarray], k: int) -> List[np.ndarray]:
    if len(frames) <= k:
        return list(frames)
    idx = np.linspace(0, len(frames) - 1, k).round().astype(int)
    return [frames[i] for i in idx]


def _subsample_timed(
    frames: List[np.ndarray], times: List[float], metadata: List[dict], k: int
) -> tuple[List[np.ndarray], List[float], List[dict]]:
    if not (len(frames) == len(times) == len(metadata)):
        raise ValueError("frames, frame_times, and frame_meta must have the same length")
    if len(frames) <= k:
        return list(frames), [float(t) for t in times], [dict(m) for m in metadata]
    idx = np.linspace(0, len(frames) - 1, k).round().astype(int)
    return (
        [frames[i] for i in idx],
        [float(times[i]) for i in idx],
        [dict(metadata[i]) for i in idx],
    )


def _greedy_group(items: List[MemoryItem], sim_th: float = 0.6) -> List[List[MemoryItem]]:
    groups: List[List[MemoryItem]] = []
    for m in items:
        if groups and float(np.dot(groups[-1][-1].embedding, m.embedding)) > sim_th:
            groups[-1].append(m)
        else:
            groups.append([m])
    return groups
