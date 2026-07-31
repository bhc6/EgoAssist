"""可插拔 VLM 后端接口。

上层（memory / retrieval / abstention / qa）只依赖本抽象类，
换后端不改上层逻辑。任何新后端实现下面 4 个方法即可。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Sequence

import numpy as np


@dataclass
class Answer:
    """VLM 对一个查询在给定证据下的回答。

    confidence 是 [0,1] 的自评置信度，作为拒答的 nonconformity 依据；
    真实后端可用 token logprob / verbalized confidence / self-consistency 估计。
    """
    text: str
    confidence: float


class VLMBackend(ABC):
    """冻结 VLM 后端的统一接口。"""

    #: 嵌入维度
    dim: int = 64

    @abstractmethod
    def embed_frames(self, frames: Sequence[np.ndarray]) -> np.ndarray:
        """把若干帧编码成单个 L2 归一化向量（关键帧/事件级表示）。"""

    def embed_frames_each(self, frames: Sequence[np.ndarray]) -> np.ndarray:
        """逐帧 L2 嵌入（不做多帧平均），shape (N, D)。

        默认实现：逐帧调用 embed_frames()。GPU 后端可覆盖此方法，
        把所有帧合并成一次 batch forward，供 localize() 减少 GPU 调用次数。
        """
        if not frames:
            return np.empty((0, self.dim), dtype=np.float32)
        return np.stack([self.embed_frames([f]) for f in frames])

    @abstractmethod
    def embed_text(self, text: str) -> np.ndarray:
        """把文本查询编码成同一空间的 L2 归一化向量。"""

    @abstractmethod
    def caption(self, frames: Sequence[np.ndarray]) -> str:
        """给事件生成一句自然语言摘要（写入记忆时用）。"""

    @abstractmethod
    def answer(self, query: str, evidence_texts: List[str],
               evidence_frames: Sequence[np.ndarray]) -> Answer:
        """基于检索到的证据回答查询，并给出自评置信度。"""

    # --- 公用小工具 ---
    @staticmethod
    def _l2(v: np.ndarray) -> np.ndarray:
        n = np.linalg.norm(v) + 1e-8
        return (v / n).astype(np.float32)
