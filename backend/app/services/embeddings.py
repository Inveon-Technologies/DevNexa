"""Text embeddings for incident retrieval.

The default embedder is a dependency-free feature-hashing model (unigrams + bigrams, signed hashing,
L2-normalised). It is deterministic and works offline, which keeps experiments reproducible. Setting
``DEVNEXA_EMBEDDING_PROVIDER=sentence-transformers`` switches to a neural model if the package is
installed; the vector dimension is projected/padded to ``embedding_dim`` so pgvector columns stay valid.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from functools import lru_cache

import numpy as np

from ..config import get_settings

_TOKEN = re.compile(r"[a-z_][a-z0-9_.]{1,}|\d{3}")
_STOP = {
    "the", "and", "for", "with", "from", "after", "on", "in", "of", "to", "is", "was", "at", "by",
    "a", "an", "be", "this", "that", "are", "not", "has", "have",
}


def tokenize(text: str) -> list[str]:
    tokens = [t.strip(".") for t in _TOKEN.findall(text.lower())]
    return [t for t in tokens if t and t not in _STOP]


def _bucket(feature: str, dim: int) -> tuple[int, float]:
    h = hashlib.blake2b(feature.encode(), digest_size=8).digest()
    n = int.from_bytes(h, "little")
    return n % dim, (1.0 if (n >> 63) & 1 else -1.0)


class HashingEmbedder:
    name = "feature-hashing"

    def __init__(self, dim: int):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = np.zeros(self.dim, dtype=np.float64)
        toks = tokenize(text)
        feats = toks + [f"{a}__{b}" for a, b in zip(toks, toks[1:])]
        for f in feats:
            idx, sign = _bucket(f, self.dim)
            vec[idx] += sign * (1.0 if "__" not in f else 0.7)
        # sub-linear term frequency
        vec = np.sign(vec) * np.log1p(np.abs(vec))
        norm = np.linalg.norm(vec)
        return (vec / norm).tolist() if norm else vec.tolist()


class SentenceTransformerEmbedder:  # pragma: no cover - optional dependency
    def __init__(self, dim: int, model: str):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model)
        self.dim = dim
        self.name = f"sentence-transformers/{model}"

    def embed(self, text: str) -> list[float]:
        v = np.asarray(self.model.encode(text, normalize_embeddings=True), dtype=np.float64)
        if v.shape[0] >= self.dim:
            v = v[: self.dim]
        else:
            v = np.pad(v, (0, self.dim - v.shape[0]))
        n = np.linalg.norm(v)
        return (v / n).tolist() if n else v.tolist()


@lru_cache
def get_embedder():
    dim = get_settings().embedding_dim
    provider = os.getenv("DEVNEXA_EMBEDDING_PROVIDER", "hashing")
    if provider == "sentence-transformers":  # pragma: no cover
        try:
            return SentenceTransformerEmbedder(dim, os.getenv("DEVNEXA_EMBEDDING_MODEL", "all-MiniLM-L6-v2"))
        except Exception:
            pass
    return HashingEmbedder(dim)


def cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a), np.asarray(b)
    d = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(va @ vb / d) if d else 0.0


def softmax(scores: dict[str, float], temperature: float = 1.0) -> dict[str, float]:
    if not scores:
        return {}
    m = max(scores.values())
    exps = {k: math.exp((v - m) / temperature) for k, v in scores.items()}
    z = sum(exps.values())
    return {k: v / z for k, v in exps.items()}
