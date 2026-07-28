"""离线确定性 Mock 后端。

不依赖任何 API / GPU / 权重，用共享概念空间（data/concepts.py）模拟一个
"冻结 VLM"：帧编码解码概念签名，文本编码解析概念词，回答复述证据 caption
并给出与检索相似度挂钩的自评置信度。足以把整条链路和 risk-coverage 评测跑通。
"""

from __future__ import annotations

from typing import List, Sequence

import numpy as np

from ..data import concepts as C
from .base import Answer, VLMBackend


class MockBackend(VLMBackend):
    dim = C.DIM

    def __init__(self, noise: float = 0.05, seed: int = 0):
        self.noise = noise
        self.rng = np.random.default_rng(seed)

    # ---- 帧编码：解码每帧签名里的概念 id，查表求平均 ----
    def _frame_vec(self, frame: np.ndarray) -> np.ndarray:
        # 约定：frame[0,0,2]==MAGIC 时，(frame[0,0,0]*256+frame[0,0,1]) 为概念 id
        if frame.ndim == 3 and frame.shape[0] > 0 and int(frame[0, 0, 2]) == C.MAGIC:
            cid = int(frame[0, 0, 0]) * 256 + int(frame[0, 0, 1])
            return C.id_to_vector(cid)
        return np.zeros(self.dim, dtype=np.float32)

    def embed_frames(self, frames: Sequence[np.ndarray]) -> np.ndarray:
        if len(frames) == 0:
            return np.zeros(self.dim, dtype=np.float32)
        v = np.mean([self._frame_vec(f) for f in frames], axis=0)
        v = v + self.noise * self.rng.standard_normal(self.dim).astype(np.float32)
        return self._l2(v)

    def embed_text(self, text: str) -> np.ndarray:
        names = C.parse_concepts(text)
        if not names:
            return np.zeros(self.dim, dtype=np.float32)
        v = np.sum([C.concept_vector(n) for n in names], axis=0)
        return self._l2(v)

    def caption(self, frames: Sequence[np.ndarray]) -> str:
        ids = []
        for f in frames:
            if f.ndim == 3 and int(f[0, 0, 2]) == C.MAGIC:
                cid = int(f[0, 0, 0]) * 256 + int(f[0, 0, 1])
                if cid > 0:
                    ids.append(C.CONCEPTS[cid - 1])
        uniq = list(dict.fromkeys(ids))
        if not uniq:
            return "background / no salient object"
        return "saw " + ", ".join(uniq)

    def answer(self, query: str, evidence_texts: List[str],
               evidence_frames: Sequence[np.ndarray]) -> Answer:
        # mock 的回答 = 复述最相关证据；置信度 = 查询与证据帧的余弦相似度（+微噪声）
        q = self.embed_text(query)
        if len(evidence_frames) == 0 or not np.any(q):
            return Answer(text="I don't have evidence for that.", confidence=0.0)
        ev = self.embed_frames(evidence_frames)
        sim = float(np.dot(q, ev))               # [-1,1]
        conf = max(0.0, min(1.0, 0.5 * (sim + 1.0)))
        conf = float(np.clip(conf + self.noise * self.rng.standard_normal(), 0, 1))
        text = evidence_texts[0] if evidence_texts else "(no caption)"
        return Answer(text=text, confidence=conf)
