"""SQLite dev-embedding-cache coverage (ADR 0043): hit/miss accounting,
partial-batch miss (some texts cached, some not), and the cache key including
model+dim so a config change doesn't serve a stale vector. Gemini itself is
monkeypatched; only the disk cache logic under test is real.
"""
import pytest

import config.vector_settings as vector_settings
from modules.vector_search import dev_cache, gemini_client

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_cache, "CACHE_PATH", str(tmp_path / "embeddings.sqlite"))
    dev_cache.hits = 0
    dev_cache.misses = 0
    yield


def _fake_embed_batch(calls):
    async def _fn(texts):
        calls.append(list(texts))
        return [[float(len(t)), 0.0] for t in texts]

    return _fn


async def test_miss_then_hit(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch(calls))

    v1 = await dev_cache.embed_batch_cached(["hello"])
    v2 = await dev_cache.embed_batch_cached(["hello"])

    assert v1 == v2
    assert calls == [["hello"]]
    assert dev_cache.hits == 1
    assert dev_cache.misses == 1


async def test_partial_batch_hit_only_calls_gemini_for_misses(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch(calls))

    await dev_cache.embed_batch_cached(["a", "b"])
    calls.clear()

    result = await dev_cache.embed_batch_cached(["a", "b", "c"])
    assert calls == [["c"]]
    assert result[0] == [1.0, 0.0]  # "a" served from cache
    assert result[2] == [1.0, 0.0]  # "c" freshly embedded


async def test_cache_key_includes_model_and_dim(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch(calls))

    await dev_cache.embed_batch_cached(["same"])
    monkeypatch.setattr(vector_settings, "GEMINI_EMBED_MODEL", "some-other-model")
    calls.clear()
    await dev_cache.embed_batch_cached(["same"])

    assert calls == [["same"]]  # model changed -> cache miss, not stale hit


async def test_embed_query_cached_delegates_to_batch(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch(calls))

    result = await dev_cache.embed_query_cached("solo")
    assert result == [4.0, 0.0]
    assert calls == [["solo"]]


async def test_clear_cache_removes_file(tmp_path, monkeypatch):
    path = tmp_path / "embeddings.sqlite"
    monkeypatch.setattr(dev_cache, "CACHE_PATH", str(path))
    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch([]))

    await dev_cache.embed_batch_cached(["x"])
    assert path.exists()
    dev_cache.clear_cache()
    assert not path.exists()
