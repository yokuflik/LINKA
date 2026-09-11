import os

# --- Server-side message search (ADR 0040) ---
# Keyword search over messages.content via Postgres native FTS (a `content_tsv`
# tsvector kept by a trigger + one btree_gin `gin(chat_id, content_tsv)` partial
# index). No external search service. Two surfaces: in-chat (one chat_id) and
# global (every chat the caller is a current member of, enforced by a
# `participants` JOIN in the query). All rate limits are Redis sliding windows
# (rlsw:) except the per-IP ceiling (fixed window); every search query also runs
# under a per-statement timeout so one pathological scan can't peg the box.

# Shortest accepted query (after trim). Anything shorter -> HTTP 422 with no DB
# hit, killing as-you-type spam at the cheapest possible point. The client is
# also expected to debounce the search box (>= 400 ms).
SEARCH_MIN_QUERY_LEN = int(os.environ.get("SEARCH_MIN_QUERY_LEN", "2"))
# Longest query string we bother parsing (a giant tsquery is its own DoS).
SEARCH_MAX_QUERY_LEN = int(os.environ.get("SEARCH_MAX_QUERY_LEN", "128"))
# Page size clamp for the cursor endpoints ([1, MAX]).
SEARCH_MAX_PAGE_SIZE = int(os.environ.get("SEARCH_MAX_PAGE_SIZE", "50"))
SEARCH_DEFAULT_PAGE_SIZE = int(os.environ.get("SEARCH_DEFAULT_PAGE_SIZE", "20"))
# `/messages/around/{id}` context window ([1, MAX] messages on each side).
SEARCH_AROUND_MAX_RADIUS = int(os.environ.get("SEARCH_AROUND_MAX_RADIUS", "50"))
SEARCH_AROUND_DEFAULT_RADIUS = int(os.environ.get("SEARCH_AROUND_DEFAULT_RADIUS", "25"))
# Chars of context kept on each side of the first match in a result snippet.
SEARCH_SNIPPET_RADIUS = int(os.environ.get("SEARCH_SNIPPET_RADIUS", "80"))
# Below this many chats for the caller, global search also passes
# `chat_id = ANY(:ids)` as a planner hint alongside the participants JOIN.
SEARCH_ANY_INLINE_MAX = int(os.environ.get("SEARCH_ANY_INLINE_MAX", "50"))
# Per-search-query statement timeout (SET LOCAL statement_timeout).
SEARCH_STATEMENT_TIMEOUT_MS = int(os.environ.get("SEARCH_STATEMENT_TIMEOUT_MS", "3000"))

# --- SSE stream (GET /search/messages/stream) ---
# Rows are read from a server-side cursor in batches of this size and yielded as
# `event: match` frames; the app process never holds the whole result set.
SEARCH_STREAM_BATCH = int(os.environ.get("SEARCH_STREAM_BATCH", "100"))
# Hard caps on one stream: it ends with `event: done {truncated: true}` when
# either is hit, keeping the read transaction short on a 1-CPU host.
SEARCH_STREAM_MAX_RESULTS = int(os.environ.get("SEARCH_STREAM_MAX_RESULTS", "500"))
SEARCH_STREAM_MAX_SECONDS = int(os.environ.get("SEARCH_STREAM_MAX_SECONDS", "20"))
# `: keepalive` comment frame cadence so an idle proxy doesn't drop the stream.
SEARCH_STREAM_KEEPALIVE_SECONDS = int(os.environ.get("SEARCH_STREAM_KEEPALIVE_SECONDS", "15"))
# One in-flight stream per user (Redis SETNX lock, this TTL). A second concurrent
# stream -> HTTP 409.
SEARCH_STREAM_LOCK_TTL_SECONDS = int(os.environ.get("SEARCH_STREAM_LOCK_TTL_SECONDS", "30"))

# --- Rate limits ---
# `search_query` two-tier: a tight per-second-ish bucket plus a sustained-spam
# ceiling. Both must pass. Applied to the cursor endpoints (in-chat, global,
# around).
SEARCH_QUERY_RATE_MAX = int(os.environ.get("SEARCH_QUERY_RATE_MAX", "10"))
SEARCH_QUERY_RATE_WINDOW_SECONDS = int(os.environ.get("SEARCH_QUERY_RATE_WINDOW_SECONDS", "10"))
SEARCH_QUERY_BURST_MAX = int(os.environ.get("SEARCH_QUERY_BURST_MAX", "30"))
SEARCH_QUERY_BURST_WINDOW_SECONDS = int(os.environ.get("SEARCH_QUERY_BURST_WINDOW_SECONDS", "60"))
# `search_stream` per user - a stream is heavy and long-lived.
SEARCH_STREAM_RATE_MAX = int(os.environ.get("SEARCH_STREAM_RATE_MAX", "3"))
SEARCH_STREAM_RATE_WINDOW_SECONDS = int(os.environ.get("SEARCH_STREAM_RATE_WINDOW_SECONDS", "60"))
# Shared per-IP ceiling across every search route (fixed window).
SEARCH_IP_RATE_MAX = int(os.environ.get("SEARCH_IP_RATE_MAX", "60"))
SEARCH_IP_RATE_WINDOW_SECONDS = int(os.environ.get("SEARCH_IP_RATE_WINDOW_SECONDS", "60"))

__all__ = [
    "SEARCH_MIN_QUERY_LEN",
    "SEARCH_MAX_QUERY_LEN",
    "SEARCH_MAX_PAGE_SIZE",
    "SEARCH_DEFAULT_PAGE_SIZE",
    "SEARCH_AROUND_MAX_RADIUS",
    "SEARCH_AROUND_DEFAULT_RADIUS",
    "SEARCH_SNIPPET_RADIUS",
    "SEARCH_ANY_INLINE_MAX",
    "SEARCH_STATEMENT_TIMEOUT_MS",
    "SEARCH_STREAM_BATCH",
    "SEARCH_STREAM_MAX_RESULTS",
    "SEARCH_STREAM_MAX_SECONDS",
    "SEARCH_STREAM_KEEPALIVE_SECONDS",
    "SEARCH_STREAM_LOCK_TTL_SECONDS",
    "SEARCH_QUERY_RATE_MAX",
    "SEARCH_QUERY_RATE_WINDOW_SECONDS",
    "SEARCH_QUERY_BURST_MAX",
    "SEARCH_QUERY_BURST_WINDOW_SECONDS",
    "SEARCH_STREAM_RATE_MAX",
    "SEARCH_STREAM_RATE_WINDOW_SECONDS",
    "SEARCH_IP_RATE_MAX",
    "SEARCH_IP_RATE_WINDOW_SECONDS",
]
