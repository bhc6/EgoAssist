"""Optional semantic channel for TimelineMemory.

TimelineMemory is lexical by default. Lexical recall cannot bridge paraphrase
("钥匙放哪了" vs "我把门卡搁在玄关柜上"), which the EgoLife backtests measured as the
dominant loss. An ``Embedder`` adds a dense channel that:

* nominates candidates from the whole scoped corpus (so a paraphrased target is
  not lost to the bounded lexical scan), and
* contributes a score, so a card with zero lexical overlap can still be
  selected when it is semantically close enough.

Eligibility (scope, time, reference, subject) is never relaxed by this channel;
it only changes *which* cards are considered and how they rank.

Vectors live in ``memory_vectors`` with ``ON DELETE CASCADE`` to the card, so
deletion, retention and ``forget_sources`` remove them with the card.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Callable, Protocol, Sequence

import numpy as np


class Embedder(Protocol):
    name: str
    dim: int

    def __call__(self, texts: Sequence[str], *, role: str = "document") -> np.ndarray: ...


def _unit(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class HashingEmbedder:
    """Deterministic, dependency-free character n-gram hashing embedder.

    Not semantic. It exists so the channel can be exercised offline and in
    tests; use a real model (``DashScopeEmbedder``) for experiments.
    """

    def __init__(self, dim: int = 512, ngram: tuple[int, ...] = (1, 2, 3)) -> None:
        self.dim = int(dim)
        self.ngram = ngram
        self.name = f"hashing-{self.dim}"

    def __call__(self, texts: Sequence[str], *, role: str = "document") -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            value = "".join(str(text).casefold().split())
            for n in self.ngram:
                for i in range(max(0, len(value) - n + 1)):
                    digest = hashlib.blake2b(value[i:i + n].encode("utf-8"), digest_size=8).digest()
                    index = int.from_bytes(digest[:4], "little") % self.dim
                    sign = 1.0 if digest[4] & 1 else -1.0
                    out[row, index] += sign
        return _unit(out)


class DashScopeEmbedder:
    """Qwen ``text-embedding-v3`` via DashScope (the model used in prior EgoLife runs)."""

    def __init__(self, model: str = "text-embedding-v3", dim: int = 1024, api_key: str | None = None,
                 batch_size: int = 10, timeout: float = 30.0) -> None:
        self.model, self.dim, self.batch_size, self.timeout = model, int(dim), int(batch_size), timeout
        self.api_key = api_key or os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        if not self.api_key:
            raise ValueError("QWEN_API_KEY or DASHSCOPE_API_KEY is required for DashScopeEmbedder")
        self.name = f"dashscope-{model}-{self.dim}"

    def __call__(self, texts: Sequence[str], *, role: str = "document") -> np.ndarray:
        import requests

        url = os.getenv("DASHSCOPE_HTTP_BASE_URL", "https://dashscope.aliyuncs.com/api/v1").rstrip("/") + \
            "/services/embeddings/text-embedding/text-embedding"
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = [str(t)[:2000] or " " for t in texts[start:start + self.batch_size]]
            response = requests.post(url, timeout=self.timeout, headers={"Authorization": f"Bearer {self.api_key}"},
                                     json={"model": self.model, "input": {"texts": batch},
                                           "parameters": {"dimension": self.dim,
                                                          "text_type": "query" if role == "query" else "document"}})
            response.raise_for_status()
            rows = sorted(response.json()["output"]["embeddings"], key=lambda r: r["text_index"])
            vectors.extend(r["embedding"] for r in rows)
        return _unit(np.asarray(vectors, dtype=np.float32))


def embedder_from_env() -> Embedder | None:
    """EGOASSIST_MEMORY_EMBEDDER = none (default) | hashing | dashscope."""
    choice = os.getenv("EGOASSIST_MEMORY_EMBEDDER", "none").strip().casefold()
    if choice in {"", "none", "off", "0"}:
        return None
    if choice == "hashing":
        return HashingEmbedder()
    if choice == "dashscope":
        return DashScopeEmbedder(model=os.getenv("EGOASSIST_MEMORY_EMBED_MODEL", "text-embedding-v3"))
    raise ValueError("EGOASSIST_MEMORY_EMBEDDER must be none, hashing or dashscope")


VECTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_vectors (
    card_id TEXT PRIMARY KEY REFERENCES memory_cards(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
);
"""


def encode(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def decode(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)


def top_k(query: np.ndarray, ids: list[str], matrix: np.ndarray, k: int) -> list[tuple[str, float]]:
    if not ids:
        return []
    scores = matrix @ _unit(query.reshape(1, -1))[0]
    k = min(k, len(ids))
    order = np.argpartition(-scores, k - 1)[:k]
    order = order[np.argsort(-scores[order])]
    return [(ids[i], float(scores[i])) for i in order]
