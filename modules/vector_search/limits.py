"""Injectable semantic-search tunables (ADR 0033 / ADR 0042 pattern)."""

from dataclasses import dataclass

from config import settings


@dataclass(frozen=True)
class VectorSearchLimits:
    min_query_len: int = settings.VECTOR_SEARCH_MIN_QUERY_LEN
    max_query_len: int = settings.VECTOR_SEARCH_MAX_QUERY_LEN
    default_limit: int = settings.VECTOR_SEARCH_DEFAULT_LIMIT
    max_limit: int = settings.VECTOR_SEARCH_MAX_LIMIT
    max_distance: float = settings.VECTOR_SEARCH_MAX_DISTANCE
    max_distance_expanded: float = settings.VECTOR_SEARCH_MAX_DISTANCE_EXPANDED
    rate_max: int = settings.VECTOR_SEARCH_RATE_MAX
    rate_window_s: int = settings.VECTOR_SEARCH_RATE_WINDOW_SECONDS
    queue_flush_size: int = settings.VECTOR_QUEUE_FLUSH_SIZE
    gemini_batch_size: int = settings.VECTOR_GEMINI_BATCH_SIZE


DEFAULT_VECTOR_SEARCH_LIMITS = VectorSearchLimits()
