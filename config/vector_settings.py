import os

# --- Semantic vector search (ADR 0042) ---
# Gemini text-embedding-004 (free tier) + pgvector IVFFlat. Empty GEMINI_API_KEY
# disables the feature cleanly (503 at the endpoint) rather than erroring.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_BASE = os.environ.get(
    "GEMINI_API_BASE", "https://generativelanguage.googleapis.com"
)
GEMINI_EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
VECTOR_EMBEDDING_DIM = int(os.environ.get("VECTOR_EMBEDDING_DIM", "768"))
# Seconds to wait for one Gemini HTTP call before giving up on that batch.
GEMINI_HTTP_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_HTTP_TIMEOUT_SECONDS", "20"))

# --- IVFFlat index (ADR 0042) ---
# Rejected HNSW: its in-RAM build graph risks OOM-killing the 1GB demo host.
# `lists` is only used when the index is (re)built by the seed / backfill
# script - not read at query time.
VECTOR_IVFFLAT_LISTS = int(os.environ.get("VECTOR_IVFFLAT_LISTS", "100"))

# --- Flush-on-demand queue (Redis list) ---
VECTOR_QUEUE_KEY = os.environ.get("VECTOR_QUEUE_KEY", "vector_embed_queue")
# Auto-flush trigger: fires a background flush once the queue reaches this size.
VECTOR_QUEUE_FLUSH_SIZE = int(os.environ.get("VECTOR_QUEUE_FLUSH_SIZE", "50"))
# One Gemini batchEmbedContents call covers at most this many texts. The
# free-tier quota (EmbedContentRequestsPerMinutePerUserPerProjectPerModel) is
# 100/minute and counts each item inside a batch call as its own request, so
# 100 leaves zero margin against any other traffic in the same minute - kept
# below that.
VECTOR_GEMINI_BATCH_SIZE = int(os.environ.get("VECTOR_GEMINI_BATCH_SIZE", "90"))

# --- Search endpoint ---
VECTOR_SEARCH_DEFAULT_LIMIT = int(os.environ.get("VECTOR_SEARCH_DEFAULT_LIMIT", "10"))
VECTOR_SEARCH_MAX_LIMIT = int(os.environ.get("VECTOR_SEARCH_MAX_LIMIT", "30"))
# Cosine-distance ceiling (lower = stricter): rows with `embedding <=> query`
# at or above this are dropped rather than padding out LIMIT with irrelevant
# results. Validated against the seeded mock corpus (gemini-embedding-001):
# genuinely relevant queries landed at 0.237-0.322 distance, unrelated ones at
# 0.436-0.488 - a clean gap with no overlap. 0.35 sits in that gap with margin
# on both sides. See docs/adr/0042.
VECTOR_SEARCH_MAX_DISTANCE = float(os.environ.get("VECTOR_SEARCH_MAX_DISTANCE", "0.35"))
# Looser ceiling for the opt-in "show more results" expansion (client passes
# expanded=true) - surfaces weaker matches the default hides, most commonly
# cross-lingual queries against this corpus (Hebrew query / English content
# measured at 0.167-0.378, still below unrelated-query floor 0.436-0.500).
# 0.42 sits just under that unrelated floor with margin. Re-validate both
# constants together if the embedding model or corpus changes materially.
VECTOR_SEARCH_MAX_DISTANCE_EXPANDED = float(
    os.environ.get("VECTOR_SEARCH_MAX_DISTANCE_EXPANDED", "0.42")
)
VECTOR_SEARCH_MIN_QUERY_LEN = int(os.environ.get("VECTOR_SEARCH_MIN_QUERY_LEN", "2"))
VECTOR_SEARCH_MAX_QUERY_LEN = int(os.environ.get("VECTOR_SEARCH_MAX_QUERY_LEN", "512"))
# Sliding-window per-user rate limit (semantic search is one Gemini call + one
# DB scan per request - tighter than keyword search's SEARCH_QUERY_RATE_*).
VECTOR_SEARCH_RATE_MAX = int(os.environ.get("VECTOR_SEARCH_RATE_MAX", "10"))
VECTOR_SEARCH_RATE_WINDOW_SECONDS = int(
    os.environ.get("VECTOR_SEARCH_RATE_WINDOW_SECONDS", "60")
)

__all__ = [
    "GEMINI_API_KEY",
    "GEMINI_API_BASE",
    "GEMINI_EMBED_MODEL",
    "VECTOR_EMBEDDING_DIM",
    "GEMINI_HTTP_TIMEOUT_SECONDS",
    "VECTOR_IVFFLAT_LISTS",
    "VECTOR_QUEUE_KEY",
    "VECTOR_QUEUE_FLUSH_SIZE",
    "VECTOR_GEMINI_BATCH_SIZE",
    "VECTOR_SEARCH_DEFAULT_LIMIT",
    "VECTOR_SEARCH_MAX_LIMIT",
    "VECTOR_SEARCH_MAX_DISTANCE",
    "VECTOR_SEARCH_MAX_DISTANCE_EXPANDED",
    "VECTOR_SEARCH_MIN_QUERY_LEN",
    "VECTOR_SEARCH_MAX_QUERY_LEN",
    "VECTOR_SEARCH_RATE_MAX",
    "VECTOR_SEARCH_RATE_WINDOW_SECONDS",
]
