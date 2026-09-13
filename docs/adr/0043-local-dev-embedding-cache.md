# ADR 0043 — Local dev-only cache for Gemini embeddings

Status: Accepted
Date: 2026-09-12

Extends: ADR 0042 (semantic vector search) — this ADR changes nothing about
production behavior, only how `scripts/seed_vector_data.py` and manual dev
testing of semantic search consume the Gemini API locally.

## Context

`scripts/seed_vector_data.py` embeds the entire mock-data corpus every time
it is run. Combined with CLAUDE.md Rule 11 (mock data is always regenerated
from scratch, never patched), every reseed re-embeds ~1,000 messages against
Gemini's free-tier quota (100 embed sub-requests/minute, ADR 0042). Manually
exercising semantic search during dev also repeatedly re-embeds the same or
similar query strings via `embed_query`. Neither case needs a fresh Gemini
call — the same input text against the same model/dimensionality always
produces the same vector.

This is pure dev-loop cost, not a production concern: nothing here may affect
the live app, the deploy host, or the `db`/`test_db` Postgres images.

## Decision

### 1. A flag-gated cache wrapper, not a change to the real call sites' default behavior

`modules/vector_search/dev_cache.py` (new) exposes `embed_batch_cached(texts)`
and `embed_query_cached(text)`, matching `gemini_client.embed_batch` /
`embed_query`'s signatures. Both:

1. Compute `cache_key = sha256(f"{model}:{dim}:{text}")` per text.
2. Look up each key in the local SQLite cache.
3. Call the real `gemini_client` function only for the misses.
4. Write new results back to the cache.
5. Return vectors in the original input order.

`scripts/seed_vector_data.py` and `modules/vector_search/service.py` route
through this wrapper **only when `VECTOR_EMBED_CACHE=1`** is set in the
environment. That env var is never set in `docker-compose.prod.yml` or
`deploy/env.production.example` — production always calls `gemini_client`
directly, unchanged. This keeps the change strictly additive and reviewable
as dev tooling, not a modification of the ADR 0042 request path.

### 2. Storage: local SQLite file, not tracked in git

`.dev_cache/gemini_embeddings.sqlite` (path itself also env-overridable via
`VECTOR_EMBED_CACHE_PATH`, for anyone who wants it elsewhere). One table:

```sql
CREATE TABLE IF NOT EXISTS embeddings (
    cache_key   TEXT PRIMARY KEY,   -- sha256(model:dim:text)
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    text_preview TEXT,              -- first ~80 chars, debugging aid only
    vector      BLOB NOT NULL,      -- packed float32, struct.pack
    created_at  TEXT NOT NULL
);
```

Vectors are packed as raw `float32` bytes via the stdlib `struct`/`array`
modules — no numpy dependency added for this. `.dev_cache/` is added to
`.gitignore`: it is regenerable, potentially large, and not meaningfully
different in kind from the mock data it caches embeddings for (also never
committed). No TTL/expiry — a cache hit is valid indefinitely for a fixed
`(model, dim, text)` triple; changing `GEMINI_EMBED_MODEL` or
`VECTOR_EMBEDDING_DIM` naturally misses the old entries since they're part of
the key, never serving a stale-shaped vector.

### 3. Observability and manual invalidation

- `scripts/seed_vector_data.py` prints a cache hit/miss summary (count saved)
  when the flag is on, so the savings are visible, not just assumed.
- A `--clear-cache` flag on the seed script deletes the SQLite file, for the
  rare case a clean re-embed is wanted.

## Consequences

- Zero risk to production: the cache path is inert unless
  `VECTOR_EMBED_CACHE=1` is explicitly exported, and that variable is not
  present in any prod/deploy config.
- Re-running `scripts/seed_vector_data.py` after a `seed_mock_data.py` reseed
  (Rule 11 regenerates mock data from scratch every time) costs Gemini calls
  only for genuinely new text, not the many messages whose content happens to
  repeat across seed runs (mock data generation reuses a bounded phrase pool).
- `.dev_cache/` is machine-local; the cache is not shared across contributors
  or CI. That's intentional — this is a personal token-saving tool, not a
  shared fixture, and committing embedding vectors derived from mock message
  content has no upside.
- **Known gap, deliberately out of scope**: no cache size cap / eviction. If
  this ever becomes a problem, clearing `.dev_cache/` is a one-command fix.
