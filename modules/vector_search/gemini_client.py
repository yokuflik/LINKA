"""Raw-httpx client for the Gemini embedding API (ADR 0042) - no Google SDK
dependency, matching the project's existing manual-HTTP style for third-party
calls (modules/auth/firebase.py).

Model: `gemini-embedding-001` (the older `text-embedding-004` name 404s on
current API keys/projects - `gemini-embedding-001` is what the ListModels
endpoint actually advertises for `embedContent`/`batchEmbedContents` now).
Its native output is 3072-dim; every call passes `outputDimensionality=768`
(Matryoshka-truncated by the API itself) to match `messages.embedding
vector(768)` - never re-embed at a different dimensionality without also
changing the column type and rebuilding the IVFFlat index.

Free-tier quota is 100 embed sub-requests/minute
(`EmbedContentRequestsPerMinutePerUserPerProjectPerModel`) - **each item
inside one `batchEmbedContents` call counts individually**, so a 100-item
batch alone can exhaust the whole per-minute quota. This module makes no
attempt to pace requests itself - the seed script keeps its batches at 90 and
sleeps a full 65s between them (see scripts/seed_vector_data.py); the runtime
flush path (50-message auto-flush / on-demand flush) makes at most one batch
call per flush, expected to stay under the ceiling at demo traffic.
"""

import logging
from typing import Sequence

import httpx

from config import settings
from modules.vector_search.errors import (
    EmbeddingProviderError,
    EmbeddingProviderQuotaExceededError,
    EmbeddingProviderUnavailableError,
)

logger = logging.getLogger(__name__)


def _require_api_key() -> str:
    if not settings.GEMINI_API_KEY:
        raise EmbeddingProviderUnavailableError("GEMINI_API_KEY is not configured")
    return settings.GEMINI_API_KEY


async def embed_batch(texts: Sequence[str]) -> list[list[float]]:
    """batchEmbedContents - up to VECTOR_GEMINI_BATCH_SIZE texts per call.
    Caller is responsible for chunking a larger batch. Raises
    EmbeddingProviderError on any HTTP/shape failure - callers decide whether
    that means "drop this batch" (flush) or "surface a 502" (query embed)."""
    api_key = _require_api_key()
    model = f"models/{settings.GEMINI_EMBED_MODEL}"
    url = f"{settings.GEMINI_API_BASE}/v1beta/{model}:batchEmbedContents"
    body = {
        "requests": [
            {
                "model": model,
                "content": {"parts": [{"text": t}]},
                "outputDimensionality": settings.VECTOR_EMBEDDING_DIM,
            }
            for t in texts
        ]
    }
    try:
        async with httpx.AsyncClient(timeout=settings.GEMINI_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, params={"key": api_key}, json=body)
        resp.raise_for_status()
        data = resp.json()
        embeddings = data["embeddings"]
        if len(embeddings) != len(texts):
            raise EmbeddingProviderError(
                f"Gemini returned {len(embeddings)} embeddings for {len(texts)} texts"
            )
        return [e["values"] for e in embeddings]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Gemini batchEmbedContents failed: %s", exc)
        raise EmbeddingProviderError(str(exc)) from exc


async def embed_query(text: str) -> list[float]:
    """embedContent - single text, used for the search query itself."""
    api_key = _require_api_key()
    model = f"models/{settings.GEMINI_EMBED_MODEL}"
    url = f"{settings.GEMINI_API_BASE}/v1beta/{model}:embedContent"
    body = {
        "model": model,
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": settings.VECTOR_EMBEDDING_DIM,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.GEMINI_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, params={"key": api_key}, json=body)
        if resp.status_code == 429:
            logger.warning("Gemini embedContent quota exceeded (429)")
            raise EmbeddingProviderQuotaExceededError("Gemini free-tier quota exceeded")
        resp.raise_for_status()
        return resp.json()["embedding"]["values"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Gemini embedContent failed: %s", exc)
        raise EmbeddingProviderError(str(exc)) from exc
