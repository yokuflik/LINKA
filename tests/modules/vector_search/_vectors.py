"""Deterministic 768-dim test vectors for pgvector cosine-distance tests.

`unit_vector(i)` builds a one-hot-ish direction so cosine distance between
two indices is controllable: distance 0 for the same index, 1.0 (orthogonal)
for different indices, and `near(i)` gives a vector a small angle away from
`unit_vector(i)` (small but non-zero distance) to test the max_distance floor.
"""
from config.vector_settings import VECTOR_EMBEDDING_DIM


def unit_vector(i: int) -> list[float]:
    v = [0.0] * VECTOR_EMBEDDING_DIM
    v[i % VECTOR_EMBEDDING_DIM] = 1.0
    return v


def near(i: int, *, off: float = 0.05) -> list[float]:
    """A vector close to unit_vector(i) - small nonzero cosine distance."""
    v = unit_vector(i)
    j = (i + 1) % VECTOR_EMBEDDING_DIM
    v[j] = off
    return v
