"""Unit coverage for the live query-embedding LRU cache (ADR 0044).

Pure in-memory component - no DB session needed. Every test resets the
module-level cache/counters first since they're process-global state.
"""
import asyncio

import pytest
from cachetools import TTLCache

import config.vector_settings as vector_settings
from modules.vector_search import query_cache

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_cache():
    query_cache._cache = TTLCache(maxsize=query_cache._MAXSIZE, ttl=query_cache._TTL_SECONDS)
    query_cache.hits = 0
    query_cache.misses = 0
    yield


def _fake_embed_query(calls, vector=None):
    async def _fn(text):
        calls.append(text)
        return list(vector) if vector is not None else [0.1, 0.2, 0.3]

    return _fn


async def test_miss_then_hit_calls_gemini_once(monkeypatch):
    calls = []
    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _fake_embed_query(calls))

    v1 = await query_cache.embed_query_cached("hello world")
    v2 = await query_cache.embed_query_cached("hello world")

    assert v1 == v2 == [0.1, 0.2, 0.3]
    assert calls == ["hello world"]
    assert query_cache.hits == 1
    assert query_cache.misses == 1


async def test_different_text_is_a_separate_key(monkeypatch):
    calls = []
    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _fake_embed_query(calls))

    await query_cache.embed_query_cached("foo")
    await query_cache.embed_query_cached("bar")

    assert calls == ["foo", "bar"]
    assert query_cache.hits == 0
    assert query_cache.misses == 2


async def test_key_includes_model_and_dim(monkeypatch):
    calls = []
    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _fake_embed_query(calls))

    await query_cache.embed_query_cached("same text")
    monkeypatch.setattr(vector_settings, "GEMINI_EMBED_MODEL", "some-other-model")
    await query_cache.embed_query_cached("same text")

    # Model changed -> different cache key -> Gemini called again, not served stale.
    assert calls == ["same text", "same text"]
    assert query_cache.misses == 2
    assert query_cache.hits == 0


async def test_lru_eviction_at_maxsize(monkeypatch):
    calls = []
    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _fake_embed_query(calls))
    query_cache._cache = TTLCache(maxsize=2, ttl=query_cache._TTL_SECONDS)

    await query_cache.embed_query_cached("a")
    await query_cache.embed_query_cached("b")
    await query_cache.embed_query_cached("c")  # evicts "a" (oldest, never re-touched)

    calls.clear()
    await query_cache.embed_query_cached("a")  # miss again - was evicted
    assert calls == ["a"]

    calls.clear()
    await query_cache.embed_query_cached("c")  # still cached
    assert calls == []


async def test_ttl_expiry_forces_recall(monkeypatch):
    calls = []
    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _fake_embed_query(calls))
    query_cache._cache = TTLCache(maxsize=query_cache._MAXSIZE, ttl=0.05)

    await query_cache.embed_query_cached("expiring")
    await asyncio.sleep(0.1)
    await query_cache.embed_query_cached("expiring")

    assert calls == ["expiring", "expiring"]


async def test_concurrent_requests_for_same_text_do_not_corrupt_cache(monkeypatch):
    calls = []

    async def slow_embed(text):
        await asyncio.sleep(0.01)
        calls.append(text)
        return [1.0, 2.0, 3.0]

    monkeypatch.setattr(query_cache.gemini_client, "embed_query", slow_embed)

    results = await asyncio.gather(*(query_cache.embed_query_cached("same") for _ in range(10)))

    # The lock only guards the in-memory dict ops, not the Gemini call itself
    # (ADR 0044 - never block the event loop on network I/O), so concurrent
    # misses for the same key can each call Gemini. What must hold regardless:
    # no exception/corruption, every caller gets a valid vector, and the
    # cache ends up with exactly the one key afterwards.
    assert all(r == [1.0, 2.0, 3.0] for r in results)
    assert len(calls) >= 1
    assert len(query_cache._cache) == 1
