"""Live in-memory LRU cache for single-query embeddings (ADR 0044).

Independent of dev_cache.py (ADR 0043): that one is SQLite-backed, dev-only,
unbounded, and covers seeding/batch traffic. This one is memory-backed,
always on (prod included), bounded, and covers only the live
gemini_client.embed_query path used by a real search request.
"""

import asyncio
import hashlib

from cachetools import TTLCache

from config import settings
from modules.vector_search import gemini_client

# 768-dim float32 vector ~= 3 KB; 20k entries is a few tens of MB - negligible
# against the 1 GB host (see ADR 0044). Sized for hit-rate headroom, not RAM.
_MAXSIZE = 20_000
_TTL_SECONDS = 6 * 60 * 60

_cache: TTLCache = TTLCache(maxsize=_MAXSIZE, ttl=_TTL_SECONDS)
# cachetools containers are plain dict-likes, not coroutine-safe: guards
# get/set so two interleaved requests never corrupt the internal LRU/TTL
# bookkeeping. Held only around in-memory dict ops, never across the Gemini
# HTTP call itself, so it never blocks the event loop on network I/O.
_lock = asyncio.Lock()

# Cumulative counters for the process lifetime (parity with dev_cache.py).
hits = 0
misses = 0


def _cache_key(model: str, dim: int, text: str) -> str:
    return hashlib.sha256(f"{model}:{dim}:{text}".encode("utf-8")).hexdigest()


async def embed_query_cached(text: str) -> list[float]:
    """Drop-in replacement for gemini_client.embed_query, backed by the
    bounded live LRU (ADR 0044) - always active, prod included."""
    global hits, misses
    key = _cache_key(settings.GEMINI_EMBED_MODEL, settings.VECTOR_EMBEDDING_DIM, text)

    async with _lock:
        vector = _cache.get(key)
    if vector is not None:
        hits += 1
        return vector

    misses += 1
    vector = await gemini_client.embed_query(text)
    async with _lock:
        _cache[key] = vector
    return vector
