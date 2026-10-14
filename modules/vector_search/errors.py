"""Exception types for semantic vector search (ADR 0042). Mapped to HTTP in main.py."""


class EmbeddingProviderUnavailableError(Exception):
    """GEMINI_API_KEY is unset - the feature is disabled, not broken. -> HTTP 503."""
    pass


class EmbeddingProviderError(Exception):
    """Gemini returned an error / malformed response for an embedding call.

    (Batch-flush failures are requeued / dropped - see service.flush_queue
    and ADR 0107 - they never reach the HTTP layer; only the search request's
    own single embedContent call does.) -> HTTP 502.

    `status_code` is the upstream HTTP status when there was one (None for
    timeouts / network / shape errors) - lets the flush path tell a content
    problem (4xx, retry cannot heal) from a transient one (ADR 0107).
    """

    def __init__(self, message: str = "", status_code: "int | None" = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def is_client_error(self) -> bool:
        return self.status_code is not None and 400 <= self.status_code < 500


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
