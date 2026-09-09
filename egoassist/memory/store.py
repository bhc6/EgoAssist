"""分层记忆：current / short-term / long-term，带时间戳锚点。

对齐 LightMem-Ego 的三级记忆与共享时间线；写入即在线（online），
整理（consolidation）异步进行。每条记忆保留少量关键帧，供后续精确时间定位。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional  # noqa: F401 – Optional kept for subclassers

import numpy as np

from ..backends.base import VLMBackend
from ..capture.events import Segment
from ..config import Config
from ..retrieval.index import VectorIndex, build_index


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
    tier: str = "short"                                        # short / long
    child_ids: List[int] = field(default_factory=list)         # long 聚合的 short id
    wall_start: Optional[datetime] = None  # 事件开始的真实挂钟时间（带时区），None 表示未知
    wall_end: Optional[datetime] = None    # 事件结束的真实挂钟时间（带时区）


class HierarchicalMemory:
    def __init__(self, backend: VLMBackend, cfg: Config, *, index: VectorIndex | None = None):
        self.backend = backend
        self.cfg = cfg
        self._index = index or build_index(backend.dim)
        self._next_id = 0
        self.current: List[MemoryItem] = []
        self.short_term: List[MemoryItem] = []
        self.long_term: List[MemoryItem] = []
        # 完整来源注册表；活动检索仍只使用 current/short_term/long_term。
        self._items_by_id: dict[int, MemoryItem] = {}
        self._since_consolidate = 0
        self._lock = threading.RLock()
        self._consolidation_thread: threading.Thread | None = None

    # ---- 在线写入 ----
    def ingest(self, seg: Segment) -> MemoryItem:
        # Heavy VLM calls are outside the lock — they are stateless and thread-safe.
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
        with self._lock:
            item = MemoryItem(id=self._new_id(), t_start=seg.t_start, t_end=seg.t_end,
                              embedding=emb, caption=cap, keyframes=kf,
                              keyframe_times=kf_times, keyframe_meta=kf_meta,
                              audio_events=audio_events, tier="short",
                              wall_start=seg.wall_start, wall_end=seg.wall_end)
            self._items_by_id[item.id] = item
            self._index.add(item.id, emb)
            self.short_term.append(item)
            self._update_current(item)
            self._since_consolidate += 1
            if (self._since_consolidate >= self.cfg.consolidate_every
                    or len(self.short_term) > self.cfg.short_term_capacity):
                self._schedule_consolidation()
        return item

    def _update_current(self, item: MemoryItem):
        self.current.append(item)
        cutoff = item.t_end - self.cfg.current_window
        self.current = [m for m in self.current if m.t_end >= cutoff]

    def _schedule_consolidation(self) -> None:
        """Kick off consolidation in a daemon thread so ingest() is never blocked."""
        if self._consolidation_thread and self._consolidation_thread.is_alive():
            return  # previous run still in progress — skip this cycle
        t = threading.Thread(
            target=self._consolidate_background, daemon=True, name="ego-consolidate"
        )
        self._consolidation_thread = t
        t.start()

    def _consolidate_background(self) -> None:
        with self._lock:
            self.consolidate()

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
        # 合并后保留最早的 wall_start 和最晚的 wall_end
        wall_starts = [m.wall_start for m in group if m.wall_start is not None]
        wall_ends = [m.wall_end for m in group if m.wall_end is not None]
        merged = MemoryItem(
            id=self._new_id(), t_start=group[0].t_start, t_end=group[-1].t_end,
            embedding=emb.astype(np.float32), caption="; ".join(caps),
            keyframes=kf, keyframe_times=kf_times, keyframe_meta=kf_meta,
            audio_events=[event for m in group for event in m.audio_events],
            tier="long",
            child_ids=[m.id for m in group],
            wall_start=min(wall_starts) if wall_starts else None,
            wall_end=max(wall_ends) if wall_ends else None,
        )
        # Batch-remove children from index (O(N) rebuild once vs O(N·k) per-item loop)
        # Keep source records for audit and snapshot provenance.
        child_ids = [child.id for child in group]
        self._index.remove_batch(child_ids)
        self._index.add(merged.id, merged.embedding)
        return merged

    def all_items(self) -> List[MemoryItem]:
        return self.short_term + self.long_term

    def retrieve_by_vector(self, query_vec: np.ndarray, k: int, *, expand_sources: bool = False) -> List[MemoryItem]:
        """Retrieve top-k most similar items using the index."""
        with self._lock:
            # If index is out of sync (e.g., test directly sets current/short_term), rebuild
            if self._index.size() == 0 and self.all_items():
                for item in self.all_items():
                    self._items_by_id[item.id] = item
                    self._index.add(item.id, item.embedding)
            if k <= 0:
                return []
            hits = self._index.search(query_vec, k)
            items = [self._items_by_id[item_id] for item_id, _score in hits if item_id in self._items_by_id]
            if not expand_sources:
                return items
            # A centroid locates an episode; original evidence answers the question.
            sources = {}
            pending = list(items)
            visited = set()
            while pending:
                item = pending.pop()
                if item.id in visited:
                    continue
                visited.add(item.id)
                children = [self._items_by_id[cid] for cid in item.child_ids if cid in self._items_by_id]
                if children:
                    pending.extend(children)
                else:
                    sources[item.id] = item
            return sorted(sources.values(), key=lambda item: float(np.dot(query_vec, item.embedding)), reverse=True)[:k]

    def _new_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i


# ---- helpers ----
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
