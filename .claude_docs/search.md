# Message Search (ADR 0040)

Read this before touching `modules/search/`, the `messages.content_tsv` column /
its trigger / GIN index, or the search rate limits.

Decision record: `docs/adr/0040-server-side-message-search.md`.
Plan + rationale: `MESSAGE_SEARCH_PLAN.md` (root).

## What it is

Keyword search over `messages.content` (+ `media_name`), PostgreSQL-native, no
external engine. Two surfaces:

- **In-chat** — one `chat_id`, `is_participant` gate.
- **Global** — every chat the caller is a **current** member of; membership is
  enforced by a `participants` JOIN *inside* the query, so a removed member's
  chats drop out immediately (not a point-in-time id list).

Enabled by ADR 0039 (E2EE dropped → `content` is server-readable plaintext).

## Schema (`modules/search/ddl.py` — `apply_search_ddl`)

Applied by **both** `scripts/init_db.py` and `tests/conftest.py` so every env
agrees. All idempotent.

- `messages.content_tsv tsvector` — real column on the `Message` model (so
  `create_all` makes it); `ADD COLUMN IF NOT EXISTS` is the deployed-DB net.
- `messages_content_tsv_trigger()` + `trg_messages_content_tsv` — `BEFORE INSERT
  OR UPDATE OF content, media_name` row trigger on the partitioned parent
  (cascades to all partitions, present & future). `to_tsvector('simple', content
  || ' ' || media_name)`. `'simple'` = no stemming (multilingual chat). A
  `GENERATED` column was rejected (rewrites every partition).
- `ix_messages_chatid_tsv` — `gin (chat_id, content_tsv)` via `btree_gin`,
  partial `WHERE deleted_at IS NULL AND purged_at IS NULL AND sender_id IS NOT
  NULL`. One index serves both surfaces (btree_gin lets a bigint sit in the GIN;
  GIN needs no leading constant, so `content_tsv @@ q` alone still uses it). New
  weekly partitions inherit it via `CREATE TABLE ... PARTITION OF`.
- Deployed DB: `python3 -m scripts.init_db` **then** `python3 -m
  scripts.backfill_search_tsv` (per-batch `UPDATE ... WHERE content_tsv IS NULL`,
  interrupt-safe).

## `modules/search/` (ADR 0022)

- `ddl.py` — the schema statements above.
- `service.py`:
  - `build_tsquery(raw, limits) -> (fn_name, value, terms)` — trim/collapse,
    length-gate (`SearchQueryTooShortError` → **422, no DB hit**), then: a query
    of only `\w`+spaces → `to_tsquery` with the last term prefix-matched
    (`foo & bar:*`, "as you type"); anything with punctuation → `websearch_to_tsquery`
    verbatim (phrase `"..."`, `-exclude`, `or`) — no prefix.
  - `encode_cursor`/`decode_cursor` — opaque base64 of the last message id; a
    malformed cursor decodes to "from the top", never a 500.
  - `make_snippet` — app-side (first-match window ± `SEARCH_SNIPPET_RADIUS`);
    cheaper than SQL `ts_headline` on a 1-CPU host.
  - `search_in_chat` / `search_global` → `SearchResponseOut {results, next_cursor,
    has_more}`. Order **`id DESC`** (recency, not `ts_rank`). Global fetches
    `get_all_chat_ids_for_user(limit=any_inline_max+1)`; if ≤ `SEARCH_ANY_INLINE_MAX`
    it also passes `chat_id = ANY(:ids)` as a planner hint (JOIN still authoritative).
  - `messages_around` → `list[Message]` with `.status` (+ 1:1 read-receipt mask,
    ADR 0003) and presigned `.media_url` attached, like `get_message_history`.
    Keeps soft-deleted rows (tombstones).
  - `stream_search` — async generator of SSE text. Own `session_scope()` +
    `SET LOCAL statement_timeout`, server-side cursor (`stream_scalars` +
    `yield_per=SEARCH_STREAM_BATCH`). Frames: `event: match` per row,
    `event: done {count, truncated}`, `event: error`; `: keepalive` every
    `SEARCH_STREAM_KEEPALIVE_SECONDS`. Caps: `SEARCH_STREAM_MAX_RESULTS` (500)
    **and** `SEARCH_STREAM_MAX_SECONDS` (20) → `truncated: true`. Always
    releases the per-user Redis lock in `finally`.
- `crud.py` — `search_chat_messages`, `search_global_messages`,
  `stream_global_messages`, `messages_around`. All non-around queries carry the
  three partial-index predicates. Cursor adds `created_at <= id_to_datetime(cursor)
  + skew` (partition pruning, same trick as `crud_message`).
- `schemas.py` — `SearchResultOut` (lighter than `MessageOut`: no tick `status`,
  no `media_url`; has `snippet` + `media_blur_hash`), `SearchResponseOut`.
- `limits.py` — `SearchLimits` frozen dataclass (ADR 0033); `get_search_limits`
  FastAPI dep; tests `app.dependency_overrides[get_search_limits] = lambda: SearchLimits(...)`.
- `errors.py` — `SearchQueryTooShortError` (422), `SearchStreamBusyError` (409).
- `router.py` — see endpoints.

## Endpoints (registered in `main.py`)

| Route | Notes |
|---|---|
| `GET /chats/{chat_id}/messages/search?q=&cursor=&limit=` | in-chat; `limit` clamped `[1, SEARCH_MAX_PAGE_SIZE]` (50) |
| `GET /chats/{chat_id}/messages/around/{message_id}?radius=` | context window, `radius` clamped `[1, 50]`, default 25 |
| `GET /search/messages?q=&cursor=&limit=&chat_id=` | global; `chat_id` present → delegates to in-chat |
| `GET /search/messages/stream?q=` | `text/event-stream`; excluded from `_per_ip_backstop`; Caddy `handle /search/messages/stream` has `flush_interval -1` |

`/search*` added to Caddy `@api`. All `IdStr` ids as JSON strings.

## Rate limiting (`config/search_settings.py`, ADR 0033 via `SearchLimits`)

| Bucket | Key | Limit | Where |
|---|---|---|---|
| `search_query` (two-tier) | `rlsw:search_query:{uid}` + `rlsw:search_query_burst:{uid}` | 10/10 s **and** 30/60 s | `_enforce_query_limits` on the 3 cursor routes (search + around) |
| `search_stream` | `rlsw:search_stream:{uid}` | 3/60 s | stream route, before the lock |
| stream concurrency | `search:stream:active:{uid}` (SET NX EX 30) | 1/user → 409 | stream route; released by the generator's `finally` |
| `search_ip` | `ratelimit:search_ip:{ip}` (fixed) | 60/60 s | every search route |

Plus `SEARCH_MIN_QUERY_LEN` (2) → 422 before any DB work, and a per-query
`SEARCH_STATEMENT_TIMEOUT_MS` (3000) `SET LOCAL`. Client is expected to debounce
the box (≥ 400 ms). The global `_per_ip_backstop` (1000/180 s) is above all of it.

## Not done / deferred (in the ADR)

`pg_trgm` infix-substring / fuzzy · `ts_rank` sort · SSE stream UI. A
**detached** cold partition (`manage_partitions.py --cold`) leaves search
while detached.

PoC search UI (magnifying glass → centered modal, cursor-paginated,
jump-to-result) is built for **both** global (`AppHeader`) and in-chat
(`ChatHeader`, scoped to the open chat) entry points — see
`.claude_docs/frontend.md` "Message search UI".

## Semantic (vector) search — ADR 0042

Separate from FTS above: `modules/vector_search/` — `messages.embedding
vector(768)` (pgvector), Gemini `gemini-embedding-001` via raw `httpx`
(`gemini_client.py`, no SDK), IVFFlat cosine index built by
`scripts/seed_vector_data.py` **after** seed data exists (never in
`init_db.py` — needs representative rows for useful centroids). Embedding is
off the send hot path: `service.enqueue_message_for_embedding` RPUSHes to a
Redis list (`vector_embed_queue`), flushed either at `VECTOR_QUEUE_FLUSH_SIZE`
(50, background task) or synchronously on-demand before a semantic search
request (`flush_queue_if_pending`). Query membership enforced the same
`participants` JOIN pattern as FTS above. Known gaps: no retry on a failed
flush batch (that batch is dropped), no re-embed on message edit.

**Relevance floor**: `crud.semantic_search_messages` filters `embedding <=>
query < max_distance` in addition to `ORDER BY ... LIMIT` — otherwise LIMIT
always pads the page with the least-bad matches even when nothing is
actually related. `VectorSearchLimits.max_distance` ← `VECTOR_SEARCH_MAX_DISTANCE`
(default `0.35`) in `config/vector_settings.py`. Empirically validated against
the seeded mock corpus (`gemini-embedding-001`): genuinely relevant queries
landed at 0.237–0.322 distance, unrelated ones at 0.436–0.488 — a clean,
non-overlapping gap; 0.35 sits in the middle with margin both ways. Re-check
this if the embedding model or corpus content changes materially.

**"Show more results" (expanded search)**: `GET /search/semantic` takes an
`expanded=true` query param → `service.semantic_search(expanded=True)` swaps
in `VectorSearchLimits.max_distance_expanded` ← `VECTOR_SEARCH_MAX_DISTANCE_EXPANDED`
(default `0.42`) instead of the default ceiling — same query shape, just a
looser bound. Exists because the default 0.35 floor, while correct for
same-language paraphrases, silently drops some legitimate cross-lingual
matches (a Hebrew query against this English corpus can land as high as
~0.44 for a loose paraphrase). Validated with 10 Hebrew-query/English-target
pairs (batched into one Gemini call): landed at 0.167–0.378, still below the
unrelated-query floor (0.436 English / ~0.49 Hebrew control queries) — 0.42
sits just under that floor. PoC: `useSearch.js`'s `loadMoreSemanticResults`
re-fetches with `expanded=true` and replaces the list (a superset, not an
append); `SearchModal.js` renders the "Show more results" button
(`showExpandButton`/`expand-results`) under the semantic tab's results once
the default page has loaded and hasn't already been expanded for this query.

**Dev-only embedding cache (ADR 0043)**: `modules/vector_search/dev_cache.py`,
active only when `VECTOR_EMBED_CACHE=1` is exported — never set in any
prod/deploy config, so production always calls `gemini_client` directly.
Caches `sha256(model:dim:text) → vector` in a local SQLite file
(`.dev_cache/gemini_embeddings.sqlite`, git-ignored, no TTL — a hit is valid
forever for a fixed model+dim). Wired into both
`scripts/seed_vector_data.py` (saves quota across repeated reseeds of the
mock-data phrase pool — CLAUDE.md Rule 11 regenerates mock data from scratch
every run) and `service.semantic_search`'s query embedding (saves quota when
manually re-testing the same/similar search strings). `--clear-cache` flag on
the seed script wipes it.

**Live query-embedding LRU cache (ADR 0044)**:
`modules/vector_search/query_cache.py`, always active (prod **and** dev,
no flag) — independent of ADR 0043's dev cache (different storage, different
scope, see the ADR's comparison table). Wraps only
`gemini_client.embed_query` (the single-query live search path, not
`embed_batch`/seeding). `cachetools.TTLCache(maxsize=20_000, ttl=6h)` guarded
by an `asyncio.Lock` (held only around dict ops, never across the Gemini HTTP
call). Key is the same `sha256(model:dim:text)` scheme as the dev cache.
`service.semantic_search` picks exactly one of the two wrappers around
`embed_query` — `dev_cache.embed_query_cached` when `VECTOR_EMBED_CACHE=1`,
else `query_cache.embed_query_cached` — never both stacked. `hits`/`misses`
module-level counters, no admin endpoint yet.
