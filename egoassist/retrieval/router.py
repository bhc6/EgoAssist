"""检索路由：查询 -> 选记忆层 + 取候选事件。

对齐 LightMem-Ego 的"按时间跨度动态路由到合适记忆层"，而非单一 flat 检索。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List

import numpy as np

from ..backends.base import VLMBackend
from ..config import Config

if TYPE_CHECKING:
    from ..memory.store import HierarchicalMemory, MemoryItem

_RECENT_HINTS = re.compile(r"\b(just now|a moment ago|recently|earlier today|just|latest)\b", re.I)
_LONG_HINTS = re.compile(r"\b(yesterday|last week|usually|every day|habit|routine|days ago)\b", re.I)


@dataclass
class Candidate:
    item: MemoryItem
    score: float


def route_and_retrieve(query: str, mem: HierarchicalMemory, backend: VLMBackend,
                       cfg: Config) -> List[Candidate]:
    q = backend.embed_text(query)

    # 1) 使用向量索引获取候选集（比 retrieve_topk 多拿一些，为后续过滤留空间）
    candidates_raw = mem.retrieve_by_vector(q, cfg.retrieve_topk * 3)

    # 2) 路由：根据时间线索选记忆层（对候选集进行过滤）
    if _RECENT_HINTS.search(query):
        pool = [m for m in candidates_raw if m in mem.current or m in mem.short_term]
    elif _LONG_HINTS.search(query):
        pool = [m for m in candidates_raw if m in mem.long_term or m in mem.short_term]
    else:
        pool = candidates_raw

    if not pool:
        pool = candidates_raw
    pool = list({item.id: item for item in pool}.values())

    # 3) 打分：余弦相似度（已由索引提供）+ 时间近因偏置
    if not pool:
        return []
    t_max = max(m.t_end for m in pool) + 1e-6
    cands: List[Candidate] = []
    for m in pool:
        sim = float(np.dot(q, m.embedding)) if np.any(q) else 0.0
        recency = m.t_end / t_max
        cands.append(Candidate(item=m, score=sim + cfg.router_recent_bias * recency))
    cands.sort(key=lambda c: c.score, reverse=True)
    return cands[: cfg.retrieve_topk]
