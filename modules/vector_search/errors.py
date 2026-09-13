"""Exception types for semantic vector search (ADR 0042). Mapped to HTTP in main.py."""


class EmbeddingProviderUnavailableError(Exception):
    """GEMINI_API_KEY is unset - the feature is disabled, not broken. -> HTTP 503."""
    pass


class EmbeddingProviderError(Exception):
    """Gemini returned an error / malformed response for a query embedding.

    (Batch-flush failures are logged and the batch is dropped - see
    service.flush_queue - they never reach this exception; this one is only
    raised on the search request's own single embedContent call.) -> HTTP 502.
    """
    pass


class EmbeddingProviderQuotaExceededError(Exception):
    """Gemini returned HTTP 429 (free-tier per-minute quota exhausted, ADR
    0042/0043) for the query embedding call. Distinct from
    EmbeddingProviderError so the frontend can show a dev-mode-specific
    notice instead of a generic "server problem" message. -> HTTP 503.
    """
    pass


class VectorSearchQueryTooShortError(Exception):
    """`q` is shorter than VECTOR_SEARCH_MIN_QUERY_LEN. -> HTTP 422."""
    pass
