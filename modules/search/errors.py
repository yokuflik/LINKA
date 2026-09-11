"""Exception types for message search (ADR 0040). Mapped to HTTP in main.py."""


class SearchQueryTooShortError(Exception):
    """`q` is shorter than SEARCH_MIN_QUERY_LEN or parses to an empty tsquery.

    Raised before any DB work so an as-you-type client that fires on every
    keystroke is rejected at the cheapest possible point. -> HTTP 422.
    """
    pass


class SearchStreamBusyError(Exception):
    """The caller already has an in-flight SSE search stream (one per user).

    -> HTTP 409. Released when the active stream's generator closes (or its
    Redis lock TTL lapses).
    """
    pass
