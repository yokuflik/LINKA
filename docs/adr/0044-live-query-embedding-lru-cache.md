# 0044 — In-memory LRU cache for live query embeddings

Status: Accepted

## Context

Semantic search (`modules/vector_search/service.semantic_search`, ADR 0042)
calls `gemini_client.embed_query` on every request to embed the user's raw
search text. Popular/repeated query strings (exact-match text) re-pay that
HTTP round-trip and burn Gemini free-tier quota (100 embed
sub-requests/minute, ADR 0042) every time, even though the embedding for a
fixed `(model, dim, text)` is deterministic and never changes.

ADR 0043 already added a cache in front of this same call path, but it is
explicitly dev-only (`VECTOR_EMBED_CACHE=1`, SQLite file, unbounded, no TTL) —
built for deterministic reseeding and batch backfills, never armed in
production. It does not help live user traffic in prod.

## Decision

Add a second, independent cache: a bounded **in-memory LRU** over
`gemini_client.embed_query` only (single-query path, not `embed_batch` —
batch calls are internal flush/seed traffic, not repeated live queries),
active in **every** environment (prod and dev alike), with no on/off flag.

- Library: `cachetools.LRUCache`, wrapped by a small `asyncio.Lock` in
  `modules/vector_search/query_cache.py`. `cachetools` is sync-only; the lock
  makes get/set atomic across concurrent requests without blocking the event
  loop (the lock is held only for in-memory dict operations, never across the
  actual Gemini HTTP call).
- Key: `sha256(model:dim:query_text)`, same scheme as `dev_cache.py`, so a
  model/dimensionality change can never return a stale-shaped vector.
- Size: **20,000 entries**. A 768-dim float32 vector is ~3 KB; even at 20k
  entries plus key/object overhead this is on the order of 10s of MB —
  negligible against the 1 GB host budget (see ADR 0042's IVFFlat-vs-HNSW
  note for what actually threatens that budget). The size is chosen for
  hit-rate headroom, not because of a memory ceiling.
- TTL: 6 hours (`cachetools.TTLCache` semantics layered on top, see
  implementation). Unlike ADR 0043's dev cache — which intentionally never
  expires because it backs deterministic seeding — a live prod cache should
  not hold a vector forever in a long-running process; a bounded TTL caps
  staleness if `GEMINI_EMBED_MODEL`/`VECTOR_EMBEDDING_DIM` ever change without
  a process restart, at negligible extra Gemini cost.
- Scope: process-local, not Redis-backed. Simpler, and a cache miss just costs
  one extra Gemini call — not worth cross-process coordination for a 1-proc
  demo host (matches the existing single-Uvicorn-process deploy model, ADR
  0007).

## Non-goals / separation from ADR 0043

This cache and `modules/vector_search/dev_cache.py` share no code, no
storage, and no key space:

| | `dev_cache.py` (ADR 0043) | `query_cache.py` (this ADR) |
|---|---|---|
| Env | dev-only, opt-in flag | universal, always on |
| Storage | SQLite file on disk | in-process memory |
| Scope | `embed_batch` (seeding) + `embed_query` (manual dev testing) | `embed_query` only (live search requests) |
| Eviction | none (unbounded, permanent) | LRU + 6h TTL, capped at 20,000 |
| Purpose | deterministic reseed / quota-saving during dev iteration | live-traffic latency + quota reduction |

`service.semantic_search` picks **one** of the two wrappers around
`gemini_client.embed_query`, never both stacked — see implementation.

## Consequences

- Repeated live search strings (a real product pattern — trending queries,
  users re-running a search) skip the Gemini round-trip entirely, cutting
  both p99 latency and free-tier quota pressure.
- Cache lives only in the one Python process's memory; restarts clear it
  (acceptable — same cold-start cost as today).
- No invalidation path beyond TTL/LRU eviction; acceptable since the cached
  value (an embedding vector) is a pure deterministic function of
  `(model, dim, text)`.
