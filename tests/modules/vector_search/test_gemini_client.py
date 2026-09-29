"""HTTP-shape coverage for the raw-httpx Gemini embedding client (ADR 0042):
successful batch/query embed, a length-mismatch response, a 429 on the query
path, and the missing-API-key guard. httpx itself is mocked via monkeypatch
on httpx.AsyncClient.post (no real network call, no live Gemini dependency).
"""
import httpx
import pytest

import config.vector_settings as vector_settings
from modules.vector_search import gemini_client
from modules.vector_search.errors import (
    EmbeddingProviderError,
    EmbeddingProviderQuotaExceededError,
    EmbeddingProviderUnavailableError,
)

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json


def _patch_post(monkeypatch, response: _FakeResponse):
    async def _fake_post(self, url, params=None, json=None):
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setattr(vector_settings, "GEMINI_API_KEY", "test-key")
    yield


async def test_embed_batch_returns_vectors_in_order(monkeypatch):
    _patch_post(
        monkeypatch,
        _FakeResponse(200, {"embeddings": [{"values": [0.1, 0.2]}, {"values": [0.3, 0.4]}]}),
    )
    result = await gemini_client.embed_batch(["a", "b"])
    assert result == [[0.1, 0.2], [0.3, 0.4]]


async def test_embed_batch_length_mismatch_raises(monkeypatch):
    _patch_post(monkeypatch, _FakeResponse(200, {"embeddings": [{"values": [0.1, 0.2]}]}))
    with pytest.raises(EmbeddingProviderError):
        await gemini_client.embed_batch(["a", "b"])


async def test_embed_batch_http_error_raises_provider_error(monkeypatch):
    _patch_post(monkeypatch, _FakeResponse(500, {}))
    with pytest.raises(EmbeddingProviderError):
        await gemini_client.embed_batch(["a"])


async def test_embed_query_returns_vector(monkeypatch):
    _patch_post(monkeypatch, _FakeResponse(200, {"embedding": {"values": [0.5, 0.6]}}))
    result = await gemini_client.embed_query("hi")
    assert result == [0.5, 0.6]


async def test_embed_query_429_raises_quota_exceeded(monkeypatch):
    _patch_post(monkeypatch, _FakeResponse(429, {}))
    with pytest.raises(EmbeddingProviderQuotaExceededError):
        await gemini_client.embed_query("hi")


async def test_missing_api_key_raises_unavailable(monkeypatch):
    monkeypatch.setattr(vector_settings, "GEMINI_API_KEY", "")
    with pytest.raises(EmbeddingProviderUnavailableError):
        await gemini_client.embed_query("hi")
    with pytest.raises(EmbeddingProviderUnavailableError):
        await gemini_client.embed_batch(["hi"])


async def test_embed_query_malformed_response_raises_provider_error(monkeypatch):
    _patch_post(monkeypatch, _FakeResponse(200, {"unexpected": "shape"}))
    with pytest.raises(EmbeddingProviderError):
        await gemini_client.embed_query("hi")
