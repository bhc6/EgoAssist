"""检索路由：查询 -> 选记忆层 + 取候选事件。

对齐 LightMem-Ego 的"按时间跨度动态路由到合适记忆层"，而非单一 flat 检索。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, List

import numpy as np

from ..backends.base import VLMBackend
from ..config import Config

if TYPE_CHECKING:
    from ..memory.store import HierarchicalMemory, MemoryItem



@dataclass
class Candidate:
    item: MemoryItem
    score: float


def route_and_retrieve(query: str, mem: HierarchicalMemory, backend: VLMBackend,
                       cfg: Config) -> List[Candidate]:
    q = backend.embed_text(query)

    # 1) 使用向量索引获取候选集（比 retrieve_topk 多拿一些，为后续过滤留空间）
    # Storage tier is not an event date. Rank original evidence without keyword-to-tier guesses.
    pool = mem.retrieve_by_vector(q, cfg.retrieve_topk * 3, expand_sources=True)
    pool = list({item.id: item for item in pool}.values())

    # 3) 打分：纯余弦相似度（P1 核心机制；recency bias 已移除，由层路由负责时间感知）
    if not pool:
        return []
    cands: List[Candidate] = []
    for m in pool:
        sim = float(np.dot(q, m.embedding)) if np.any(q) else 0.0
        cands.append(Candidate(item=m, score=sim))
    cands.sort(key=lambda c: c.score, reverse=True)
    return cands[: cfg.retrieve_topk]
