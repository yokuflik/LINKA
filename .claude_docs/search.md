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

`pg_trgm` infix-substring / fuzzy · `ts_rank` sort · pgvector semantic search ·
SSE stream UI. A **detached** cold partition
(`manage_partitions.py --cold`) leaves search while detached.

PoC search UI (magnifying glass → centered modal, cursor-paginated,
jump-to-result) is built for **both** global (`AppHeader`) and in-chat
(`ChatHeader`, scoped to the open chat) entry points — see
`.claude_docs/frontend.md` "Message search UI".
