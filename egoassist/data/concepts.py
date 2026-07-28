"""共享概念空间（synthetic 数据与 MockBackend 都依赖它）。

为了让 MockBackend 能**离线、确定性**地产出有意义的检索/回答行为，
我们定义一个固定的"概念"词表，每个概念对应一个固定的单位向量。
- 合成帧把"概念 id"编码进像素（见 data/synthetic.py），MockBackend 解码后查表得到向量；
- 文本（caption / query）里出现的概念词，被 MockBackend 解析并求和成文本向量。
这样 embed_frames 与 embed_text 落在同一空间，检索/定位/拒答才有可评测的信号。

换成真实后端时，本文件完全用不到——它只是 mock 的"地面真值语义"。
"""

from __future__ import annotations

import numpy as np

# 一天里可能出现的日常概念（物体 / 人 / 事件）
CONCEPTS = [
    "keys", "wallet", "phone", "glasses", "backpack", "umbrella",
    "coffee_mug", "water_bottle", "laptop", "notebook", "charger",
    "desk", "hallway", "reading_room", "sofa", "door", "shelf",
    "alice", "bob", "carol",           # 对话中的人
    "meeting", "grocery", "medicine",  # 事件/活动
]

DIM = 64
MAGIC = 42  # 帧签名校验字节


def _concept_matrix(dim: int = DIM, seed: int = 20260717) -> np.ndarray:
    rng = np.random.default_rng(seed)
    m = rng.standard_normal((len(CONCEPTS), dim)).astype(np.float32)
    m /= (np.linalg.norm(m, axis=1, keepdims=True) + 1e-8)
    return m


_MAT = _concept_matrix()
_NAME2IDX = {c: i for i, c in enumerate(CONCEPTS)}


def concept_vector(name: str) -> np.ndarray:
    """概念名 -> 固定单位向量（id 0 保留给背景，返回零向量）。"""
    if name not in _NAME2IDX:
        return np.zeros(DIM, dtype=np.float32)
    return _MAT[_NAME2IDX[name]].copy()


def concept_id(name: str) -> int:
    """概念名 -> 编码 id（+1，使 0 表示背景/无概念）。"""
    return _NAME2IDX.get(name, -1) + 1


def id_to_vector(cid: int) -> np.ndarray:
    """编码 id -> 向量（0 = 背景 = 零向量）。"""
    if cid <= 0:
        return np.zeros(DIM, dtype=np.float32)
    return _MAT[cid - 1].copy()


def parse_concepts(text: str) -> list[str]:
    """从自然语言里抽出出现过的概念词（简单包含匹配，够 mock 用）。"""
    t = text.lower()
    return [c for c in CONCEPTS if c.replace("_", " ") in t or c in t]
