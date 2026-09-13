"""Local dev-only cache for Gemini embeddings (ADR 0043). Inert unless
VECTOR_EMBED_CACHE=1 - never wired into production, only saves Gemini calls
across repeated `scripts.seed_vector_data` runs and manual semantic-search
testing. Cache lives in a local SQLite file, never committed (.gitignore).
"""

import hashlib
import os
import sqlite3
import struct
import time
from typing import Sequence

from config import settings
from modules.vector_search import gemini_client

CACHE_ENABLED = os.environ.get("VECTOR_EMBED_CACHE", "") == "1"
CACHE_PATH = os.environ.get("VECTOR_EMBED_CACHE_PATH", ".dev_cache/gemini_embeddings.sqlite")

# Cumulative counters for the process lifetime, surfaced by the seed script.
hits = 0
misses = 0


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(CACHE_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embeddings (
            cache_key    TEXT PRIMARY KEY,
            model        TEXT NOT NULL,
            dim          INTEGER NOT NULL,
            text_preview TEXT,
            vector       BLOB NOT NULL,
            created_at   TEXT NOT NULL
        )
        """
    )
    return conn


def _cache_key(model: str, dim: int, text: str) -> str:
    return hashlib.sha256(f"{model}:{dim}:{text}".encode("utf-8")).hexdigest()


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def clear_cache() -> None:
    if os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)


async def embed_batch_cached(texts: Sequence[str]) -> list[list[float]]:
    """Drop-in replacement for gemini_client.embed_batch - only calls Gemini
    for texts not already in the local cache."""
    global hits, misses
    model = settings.GEMINI_EMBED_MODEL
    dim = settings.VECTOR_EMBEDDING_DIM
    keys = [_cache_key(model, dim, t) for t in texts]

    conn = _connect()
    try:
        results: list[list[float] | None] = [None] * len(texts)
        for i, key in enumerate(keys):
            row = conn.execute("SELECT vector FROM embeddings WHERE cache_key = ?", (key,)).fetchone()
            if row is not None:
                results[i] = _unpack(row[0])
                hits += 1

        miss_indices = [i for i, r in enumerate(results) if r is None]
        if miss_indices:
            misses += len(miss_indices)
            fresh = await gemini_client.embed_batch([texts[i] for i in miss_indices])
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for i, vec in zip(miss_indices, fresh):
                results[i] = vec
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (cache_key, model, dim, text_preview, vector, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (keys[i], model, dim, texts[i][:80], _pack(vec), now),
                )
            conn.commit()

        return results  # type: ignore[return-value]
    finally:
        conn.close()


async def embed_query_cached(text: str) -> list[float]:
    """Drop-in replacement for gemini_client.embed_query, same cache."""
    vectors = await embed_batch_cached([text])
    return vectors[0]
