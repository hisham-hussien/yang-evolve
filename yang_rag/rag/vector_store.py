#!/usr/bin/env python3
"""
Vector Store Backends for YANG-RAG
----------------------------------
Provides pluggable vector search backends:
- NumPy (in-memory cosine via sklearn) [default, existing path]
- FAISS (faiss-cpu) for scalable ANN search with inner product (cosine)

Other backends (Qdrant, etc.) can be added later.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
import numpy as np


@dataclass
class SearchResult:
    distances: np.ndarray  # shape (k,)
    indices: np.ndarray    # shape (k,)


class FaissStore:
    """FAISS index wrapper using inner product (IP) for cosine similarity.
    Note: Inputs must be L2-normalized for cosine==inner product.
    """
    def __init__(self, dim: int):
        self.dim = dim
        self.index = None
        self._ensure_faiss()
        import faiss
        self.faiss = faiss
        self.index = faiss.IndexFlatIP(dim)

    def _ensure_faiss(self):
        try:
            import faiss  # noqa: F401
        except Exception as e:
            raise RuntimeError(
                "FAISS backend requested but 'faiss' is not installed. Install faiss-cpu and retry."
            ) from e

    @staticmethod
    def _l2_normalize(v: np.ndarray) -> np.ndarray:
        if v.ndim == 1:
            v = v.reshape(1, -1)
        norms = np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
        return v / norms

    def build(self, embeddings: np.ndarray):
        # Build IP index on normalized embeddings
        normed = self._l2_normalize(embeddings).astype('float32')
        self.index.add(normed)

    def save(self, dir_path: Path, prefix: str = ''):
        dir_path.mkdir(parents=True, exist_ok=True)
        filename = f'{prefix}faiss.index' if prefix else 'faiss.index'
        path = dir_path / filename
        self.faiss.write_index(self.index, str(path))

    def load(self, dir_path: Path, prefix: str = ''):
        filename = f'{prefix}faiss.index' if prefix else 'faiss.index'
        path = dir_path / filename
        import faiss
        if not path.exists():
            raise FileNotFoundError(f"FAISS index not found at {path}")
        self.index = faiss.read_index(str(path))
        self.dim = self.index.d

    def search(self, query_vec: np.ndarray, top_k: int) -> SearchResult:
        q = self._l2_normalize(query_vec.astype('float32'))
        distances, indices = self.index.search(q, top_k)
        # Return the first row only (single query)
        return SearchResult(distances=distances[0], indices=indices[0])
