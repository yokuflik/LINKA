"""Injectable search tunables (ADR 0033 / ADR 0040).

`SearchLimits` is the single place these caps are named. The router exposes
`get_search_limits` as a FastAPI dependency for `app.dependency_overrides`;
service helpers take `limits: SearchLimits = DEFAULT_SEARCH_LIMITS`.
"""

from dataclasses import dataclass

from config import settings


@dataclass(frozen=True)
class SearchLimits:
    # Query-string shaping (enforced in the service, before any DB work).
    min_query_len: int = settings.SEARCH_MIN_QUERY_LEN
    max_query_len: int = settings.SEARCH_MAX_QUERY_LEN
    # Pagination / context clamps (router).
    max_page_size: int = settings.SEARCH_MAX_PAGE_SIZE
    default_page_size: int = settings.SEARCH_DEFAULT_PAGE_SIZE
    around_max_radius: int = settings.SEARCH_AROUND_MAX_RADIUS
    around_default_radius: int = settings.SEARCH_AROUND_DEFAULT_RADIUS
    snippet_radius: int = settings.SEARCH_SNIPPET_RADIUS
    any_inline_max: int = settings.SEARCH_ANY_INLINE_MAX
    statement_timeout_ms: int = settings.SEARCH_STATEMENT_TIMEOUT_MS
    # SSE stream.
    stream_batch: int = settings.SEARCH_STREAM_BATCH
    stream_max_results: int = settings.SEARCH_STREAM_MAX_RESULTS
    stream_max_seconds: int = settings.SEARCH_STREAM_MAX_SECONDS
    stream_keepalive_seconds: int = settings.SEARCH_STREAM_KEEPALIVE_SECONDS
    stream_lock_ttl_seconds: int = settings.SEARCH_STREAM_LOCK_TTL_SECONDS
    # Rate limits.
    query_rate_max: int = settings.SEARCH_QUERY_RATE_MAX
    query_rate_window_s: int = settings.SEARCH_QUERY_RATE_WINDOW_SECONDS
    query_burst_max: int = settings.SEARCH_QUERY_BURST_MAX
    query_burst_window_s: int = settings.SEARCH_QUERY_BURST_WINDOW_SECONDS
    stream_rate_max: int = settings.SEARCH_STREAM_RATE_MAX
    stream_rate_window_s: int = settings.SEARCH_STREAM_RATE_WINDOW_SECONDS
    ip_rate_max: int = settings.SEARCH_IP_RATE_MAX
    ip_rate_window_s: int = settings.SEARCH_IP_RATE_WINDOW_SECONDS


DEFAULT_SEARCH_LIMITS = SearchLimits()
