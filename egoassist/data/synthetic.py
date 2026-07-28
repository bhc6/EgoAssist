"""合成"一天"第一视角数据（全离线、可复现）。

产出：
- 一个 FrameEvent 流：若干微事件，每个事件聚焦一个概念（物体/人/活动），
  帧里编码概念签名（供 MockBackend 解码）+ 概念相关灰度（供事件分段的跨帧变化检测）。
- 一批带 ground-truth 的查询：可回答（概念出现过，附 gt 证据窗口）与
  不可回答（概念全天没出现，正确动作是拒答）。

用于把整条链路 + risk-coverage 评测跑通，并作为对接真实基准
（SuperMemory-VQA / S-EMBER / LightMem-Ego）前的冒烟测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np

from ..capture.stream import FrameEvent
from . import concepts as C

FRAME = 24  # 合成帧边长


def _concept_texture(concept: str) -> np.ndarray:
    """每个概念一张固定基底：概念专属主色调（直方图峰值位置不同 -> 事件边界可靠可分）。"""
    cid = C.concept_id(concept)
    base = int(8 + 239 * cid / (len(C.CONCEPTS) + 1))   # 主色调均匀铺开 0..255
    r = np.random.default_rng(1000 + cid)
    tex = base + r.integers(-6, 7, (FRAME, FRAME, 3))   # 峰值在 base 附近
    return tex.clip(0, 255).astype(np.uint8)


def _make_frame(concept: str, rng) -> np.ndarray:
    cid = C.concept_id(concept)                    # 0 表示背景
    f = _concept_texture(concept).copy()
    f = (f.astype(np.int16) + rng.integers(-3, 4, f.shape)).clip(0, 255).astype(np.uint8)
    f[0, 0, 0] = cid // 256                          # 概念签名（供 MockBackend 解码）
    f[0, 0, 1] = cid % 256
    f[0, 0, 2] = C.MAGIC
    return f


@dataclass
class Query:
    text: str
    answerable: bool
    gt_concept: str
    gt_t0: float
    gt_t1: float


# 全天出现的物体/场景（可回答查询从中取）
_DAY_OBJECTS = ["keys", "wallet", "phone", "glasses", "backpack",
                "coffee_mug", "laptop", "notebook", "charger", "umbrella"]
_PEOPLE = ["alice", "bob", "carol"]
# 全天不出现的概念（不可回答查询从中取）
_UNSEEN = ["medicine", "grocery", "water_bottle", "shelf", "sofa"]

_OBJ_TEMPLATES = [
    "where did I put my {c}?",
    "where is my {c}?",
    "have you seen my {c}?",
    "where did I leave my {c}?",
    "I can't find my {c}, where is it?",
    "do you know where my {c} is?",
]
_PPL_TEMPLATES = [
    "what did {c} say earlier?",
    "what did {c} tell me?",
    "do you recall what {c} said?",
    "remind me what {c} mentioned",
]


def _phrase(concept: str, k: int) -> str:
    nice = concept.replace("_", " ")
    if concept in _PEOPLE:
        tpl = _PPL_TEMPLATES[k % len(_PPL_TEMPLATES)]
    else:
        tpl = _OBJ_TEMPLATES[k % len(_OBJ_TEMPLATES)]
    return tpl.format(c=nice)


def make_day(n_events: int = 60, seed: int = 0, paraphrases: int = 6
             ) -> Tuple[List[FrameEvent], List[Query]]:
    rng = np.random.default_rng(seed)
    stream: List[FrameEvent] = []
    t = 0.0
    dt = 1.0
    last_win: dict = {}    # concept -> (t0, t1)，取最后一次出现

    for i in range(n_events):
        if i % 4 == 3:
            concept = _PEOPLE[i % len(_PEOPLE)]
            audio = f"{concept} said the meeting is at three"
        else:
            concept = _DAY_OBJECTS[i % len(_DAY_OBJECTS)]
            audio = None
        dur = int(rng.integers(3, 6))
        t_start = t
        for j in range(dur):
            stream.append(FrameEvent(t=t, frame=_make_frame(concept, rng),
                                     audio=audio if j == 0 else None,
                                     gaze=(0.5, 0.5)))
            t += dt
        last_win[concept] = (t_start, t - dt)
        t += dt  # 事件间留 1s 间隔

    # 可回答查询：每个出现过的概念生成多条改写
    queries: List[Query] = []
    for concept, (t0, t1) in last_win.items():
        for k in range(paraphrases):
            queries.append(Query(_phrase(concept, k), True, concept, t0, t1))
    # 不可回答查询：概念全天未出现，正确动作是拒答
    for c in _UNSEEN:
        for k in range(paraphrases):
            queries.append(Query(_phrase(c, k), False, c, -1.0, -1.0))

    return stream, queries
