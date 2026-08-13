"""Vector index layer.

FlatIndex   — pure numpy, zero extra deps, always available.
FAISSIndex  — CPU FAISS-backed inner-product index; requires faiss-cpu.
build_index — factory that auto-selects the best available backend.

Both classes satisfy the VectorIndex Protocol.  Vectors are assumed to be
L2-normalised before insertion; inner-product then equals cosine similarity.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import numpy as np

LOGGER = logging.getLogger(__name__)


@runtime_checkable
class VectorIndex(Protocol):
    """Minimal interface that every index implementation must satisfy."""

    def add(self, item_id: int, vector: np.ndarray) -> None: ...
    def remove_batch(self, item_ids: list[int]) -> None: ...
    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]: ...
    def size(self) -> int: ...


# ---------------------------------------------------------------------------
# FlatIndex: brute-force, numpy only
# ---------------------------------------------------------------------------

class FlatIndex:
    """Pure-numpy brute-force cosine search.  O(n) per query, no extra deps."""

    def __init__(self, dim: int) -> None:
        self.dim = int(dim)
        self._ids: list[int] = []
        self._vecs: list[np.ndarray] = []

    def add(self, item_id: int, vector: np.ndarray) -> None:
        v = np.asarray(vector, dtype=np.float32).ravel()
        if v.shape[0] != self.dim:
            raise ValueError(f"FlatIndex: expected dim={self.dim}, got {v.shape[0]}")
        self._ids.append(item_id)
        self._vecs.append(v)

    def remove_batch(self, item_ids: list[int]) -> None:
        """Remove multiple items in one pass — O(n) instead of O(n·k)."""
        to_remove = set(item_ids)
        remaining = [(i, v) for i, v in zip(self._ids, self._vecs) if i not in to_remove]
        if remaining:
            ids, vecs = zip(*remaining)
            self._ids = list(ids)
            self._vecs = list(vecs)
        else:
            self._ids, self._vecs = [], []

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        if not self._vecs:
            return []
        q = np.asarray(query, dtype=np.float32).ravel()
        mat = np.stack(self._vecs)          # (n, dim)
        scores = mat @ q                    # cosine (L2-normalised vectors)
        top_k = min(k, len(scores))
        idx = np.argpartition(scores, -top_k)[-top_k:]
        idx = idx[np.argsort(scores[idx])[::-1]]
        return [(self._ids[int(i)], float(scores[i])) for i in idx]

    def size(self) -> int:
        return len(self._ids)


# ---------------------------------------------------------------------------
# FAISSIndex: CPU FAISS inner-product index, optional
# ---------------------------------------------------------------------------

class FAISSIndex:
    """CPU FAISS-backed cosine index.

    Keeps a parallel ``_id_to_vec`` dict so vectors can be reconstructed
    cheaply on ``remove`` without relying on FAISS's limited remove support.
    Starts as IndexFlatIP; auto-upgrades to IVFFlat above *_IVF_THRESHOLD*.
    """

    _IVF_THRESHOLD = 1_000  # switch to IVFFlat once we exceed this many vectors

    def __init__(self, dim: int) -> None:
        import faiss  # noqa: PLC0415 – intentional conditional import

        self._faiss = faiss
        self.dim = int(dim)
        self._id_to_vec: dict[int, np.ndarray] = {}
        self._pos_to_id: list[int] = []     # FAISS positional row → user item_id
        self._index = faiss.IndexFlatIP(self.dim)

    def add(self, item_id: int, vector: np.ndarray) -> None:
        v = np.asarray(vector, dtype=np.float32).ravel()
        if v.shape[0] != self.dim:
            raise ValueError(f"FAISSIndex: expected dim={self.dim}, got {v.shape[0]}")
        self._id_to_vec[item_id] = v
        self._pos_to_id.append(item_id)
        self._index.add(v[np.newaxis, :])
        if len(self._pos_to_id) == self._IVF_THRESHOLD:
            self._rebuild_ivf()

    def remove_batch(self, item_ids: list[int]) -> None:
        """Remove multiple items with a single index rebuild — O(N) instead of O(N·k)."""
        removed = {i for i in item_ids if i in self._id_to_vec}
        if not removed:
            return
        for i in removed:
            del self._id_to_vec[i]
        self._pos_to_id = list(self._id_to_vec.keys())
        self._index = self._faiss.IndexFlatIP(self.dim)
        if self._pos_to_id:
            mat = np.stack([self._id_to_vec[i] for i in self._pos_to_id])
            self._index.add(mat)

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        if self._index.ntotal == 0:
            return []
        q = np.asarray(query, dtype=np.float32).ravel()[np.newaxis, :]
        k_real = min(k, int(self._index.ntotal))
        if hasattr(self._index, "nprobe") and hasattr(self._index, "nlist"):
            self._index.nprobe = max(1, int(self._index.nlist ** 0.5))
        scores, positions = self._index.search(q, k_real)
        return [
            (self._pos_to_id[int(pos)], float(score))
            for pos, score in zip(positions[0], scores[0])
            if 0 <= int(pos) < len(self._pos_to_id)
        ]

    def size(self) -> int:
        return len(self._id_to_vec)

    def _rebuild_ivf(self) -> None:
        """Replace FlatIP with IVFFlat once the collection is large enough."""
        n = len(self._pos_to_id)
        nlist = max(4, int(n ** 0.5))
        quantizer = self._faiss.IndexFlatIP(self.dim)
        ivf = self._faiss.IndexIVFFlat(quantizer, self.dim, nlist, self._faiss.METRIC_INNER_PRODUCT)
        mat = np.stack([self._id_to_vec[i] for i in self._pos_to_id])
        ivf.train(mat)
        ivf.add(mat)
        self._index = ivf


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_index(dim: int, *, force_flat: bool = False) -> VectorIndex:
    """Return a FAISSIndex if *faiss-cpu* is installed, else FlatIndex.

    Args:
        dim: Embedding dimension.  Must match the vectors you intend to add.
        force_flat: Always use FlatIndex regardless of faiss availability.
                    Useful in unit tests that do not install optional FAISS.
    """
    if not force_flat:
        try:
            return FAISSIndex(dim)
        except ImportError:
            LOGGER.warning(
                "faiss-cpu not found; falling back to FlatIndex (O(n) linear scan). "
                "Install faiss-cpu for optimized CPU vector search."
            )
    return FlatIndex(dim)
