# ADR 0040 — Server-side message search (PostgreSQL FTS)

Status: Accepted — implemented (`modules/search/`, 424 tests green)
Date: 2026-09-10

Enabled by: ADR 0039 (`messages.content` is now server-readable plaintext).
Implementation plan: `MESSAGE_SEARCH_PLAN.md` (root).

## Context

We need keyword search over message text, on two surfaces:

- **In-chat** — within one `chat_id` ("search this conversation").
- **Global** — across every chat the caller is currently a member of.

Constraints:

- Single 1 GB t3.micro demo host (ADR 0007). **No new search service** —
  Elasticsearch / OpenSearch / Meilisearch are out.
- `messages` is RANGE-partitioned by `created_at` (weekly), designed for
  tens-of-billions of rows (ADR 0005). Any index must inherit to new partitions
  and must not make the message-INSERT hot path unaffordable.
- No DB migrations (CLAUDE.md) — schema changes go through `scripts/init_db.py`
  with `IF NOT EXISTS` / `IF EXISTS`.
- Permission enforcement must be **provable at the query level** — a user
  removed from a group must not see its messages in a global search.

## Decision

### 1. Index: PostgreSQL native FTS, not trigram, not external

A `tsvector` GIN index is the primary mechanism.

- **Config `'simple'`** — no stemming, no stop-word removal. Chat is
  multilingual; language-specific stemming corrupts more than it helps, and
  common words must stay searchable.
- Rejected `pg_trgm` as primary: its GIN is ~100–200% of text volume vs.
  ~15–30% for FTS — infeasible at target scale on this host — and it scans more
  for word/prefix matches, which is the actual product need. `pg_trgm` remains a
  **documented later addition** (behind a config flag + an OR branch in the
  query builder) if infix-substring or fuzzy/typo search becomes a requirement.
- Rejected an external search service: operationally and financially out of
  scope for the demo; FTS covers the requirement.

### 2. Column + trigger (no migration, no table rewrite)

- `messages.content_tsv tsvector` — nullable, `ALTER TABLE ... ADD COLUMN IF NOT
  EXISTS` in `init_db.py`.
- Maintained by a **row trigger on the partitioned parent** (`BEFORE INSERT OR
  UPDATE OF content, media_name`), propagating to all current and future
  partitions:
  `to_tsvector('simple', coalesce(content,'') || ' ' || coalesce(media_name,''))`.
- A `GENERATED ALWAYS ... STORED` column was rejected: adding one rewrites and
  locks every partition. `CREATE OR REPLACE FUNCTION` + `CREATE TRIGGER` is
  idempotent-friendly and covers edit / restore / purge automatically
  (`purge_message` nulls `content` → the vector clears).
- **Media captions** are indexed (they live in `content`); **`media_name`**
  (filenames) is folded in. **System messages** (`sender_id IS NULL`, `type = 6`)
  are excluded — see the partial index.

### 3. One composite GIN index via `btree_gin`

```sql
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE INDEX IF NOT EXISTS ix_messages_chatid_tsv
  ON messages USING gin (chat_id, content_tsv)
  WHERE deleted_at IS NULL AND purged_at IS NULL AND sender_id IS NOT NULL;
```

- `btree_gin` makes `chat_id` an indexable GIN key, so **in-chat** search
  (`chat_id = :cid AND content_tsv @@ :q`) seeks directly with no cross-chat
  post-filter scan.
- The same index serves **global** search (`content_tsv @@ :q` alone — GIN needs
  no leading constant).
- The partial predicate keeps deleted / purged / system rows out of the index;
  every search query carries those same three predicates.
- Created on the parent → PG15 propagates to each partition; new weekly
  partitions from `manage_partitions.py` inherit it via `CREATE TABLE ...
  PARTITION OF`.
- **One** index, not a separate in-chat and global index — the write
  amplification of two billion-row GINs is the cost being avoided.

### 4. Query-level permission enforcement

- **In-chat**: `is_participant(session, chat_id, user_id)` → 403, then
  `WHERE chat_id = :chat_id`.
- **Global**: the SQL **joins `participants`**
  (`JOIN participants p ON p.chat_id = m.chat_id AND p.user_id = :user_id`) — a
  PK hit per candidate row. Membership is enforced inside the query, so every
  returned row is provably in a chat the user is a **current** member of. A
  pre-fetched `chat_id` list (point-in-time) would not guarantee that.
- When the user is in `< SEARCH_ANY_INLINE_MAX` (≈50) chats, also pass
  `AND m.chat_id = ANY(:ids)` as a planner hint (still `btree_gin`-indexable).
- No join-date floor: members see full history everywhere else in the app, so
  search is consistent with `get_chat_messages`.

### 5. Three endpoints + a context helper (`modules/search/`, ADR 0022)

| Route | Shape | Purpose |
|---|---|---|
| `GET /chats/{chat_id}/messages/search?q=&cursor=&limit=` | JSON, cursor | in-chat |
| `GET /search/messages?q=&cursor=&limit=&chat_id=` | JSON, cursor | global, **default** (`chat_id` present → delegates to in-chat) |
| `GET /search/messages/stream?q=` | `text/event-stream` (SSE) | global, huge result sets |
| `GET /chats/{chat_id}/messages/around/{message_id}?radius=` | JSON | jump-to-context (history paginates backwards only) |

Both cursor endpoints use one opaque `cursor` (base64 of the last message id);
a malformed cursor is treated as "from the top", never a 500. `build_tsquery`
runs before the membership check so a too-short query is a clean 422 even for a
non-member (membership is still enforced before the search query executes). The
SSE route is kept out of the `_per_ip_backstop` BaseHTTPMiddleware (long-lived
response) and has its own per-user + per-IP gates + a 1-in-flight-per-user lock.

- Ordering is **recency** (`id DESC` — Snowflake ids are time-sortable), not
  `ts_rank`: matches chat-search UX and streams straight off the index. Global
  results are a single `id DESC` merge across chats.
- **SSE** (not WebSocket, not a giant JSON body): a global search can match
  millions of rows across huge chats. The stream reads a **server-side cursor**
  (batch 100) and yields `event: match` frames, ending with
  `event: done {count, truncated}`; `: keepalive` every 15 s. Hard caps
  `SEARCH_STREAM_MAX_RESULTS` (500) **and** `SEARCH_STREAM_MAX_SECONDS` (20) keep
  the read transaction short on a 1-CPU box. Backpressure is automatic (the
  generator awaits the ASGI send). Membership is checked at query time only — a
  mid-stream removal is not reflected (stream is seconds-long).
- Caddy: `/search/*` proxy path gets `flush_interval -1` (no response buffering).
- Snippets are computed **app-side** (first match offset ± N chars), not SQL
  `ts_headline` (which reparses the document) — cheaper on this host.
- Search results carry `media_blur_hash` but **no presigned `media_url`** (kept
  light); real bytes load via the normal history path on jump-to-context.
- `q` shorter than `SEARCH_MIN_QUERY_LEN` (2) → **422 before any DB work**.
- Query string → `tsquery` via `websearch_to_tsquery('simple', …)` (phrase,
  AND/OR, `-exclude`) + `:*` appended to the trailing lexeme for as-you-type
  prefix match.
- Every search query runs with `SET LOCAL statement_timeout =
  SEARCH_STATEMENT_TIMEOUT_MS` (3000).

### 6. Rate limiting (Redis, ADR 0033 injectable-limits pattern)

`modules/search/limits.py::SearchLimits` + `get_search_limits` FastAPI dep;
knobs `SEARCH_*` in a new `config/search_settings.py` (ADR 0019). All → HTTP 429
via the existing handler.

| Bucket | Key | Limit |
|---|---|---|
| `search_query` (two-tier) | `rlsw:search_query:{uid}` + `…_burst:{uid}` | 10 / 10 s **and** 30 / 60 s |
| `search_stream` | `rlsw:search_stream:{uid}` | 3 / 60 s |
| stream concurrency | `search:stream:active:{uid}` (SETNX + TTL, released on close) | 1 active / user → 409 |
| `search_ip` | `ratelimit:search_ip:{ip}` (fixed) | 60 / 60 s |

Plus the 422 min-length gate and a client-side debounce ≥ 400 ms for the
as-you-type box. The global `_per_ip_backstop` (1000 / 180 s) sits above all of
this.

## Schema changes (`scripts/init_db.py`, non-`--drop`)

```
CREATE EXTENSION IF NOT EXISTS btree_gin;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS content_tsv tsvector;
CREATE OR REPLACE FUNCTION messages_content_tsv_trigger() RETURNS trigger ...;
DROP TRIGGER IF EXISTS trg_messages_content_tsv ON messages;
CREATE TRIGGER trg_messages_content_tsv BEFORE INSERT OR UPDATE OF content, media_name
  ON messages FOR EACH ROW EXECUTE FUNCTION messages_content_tsv_trigger();
CREATE INDEX IF NOT EXISTS ix_messages_chatid_tsv ON messages
  USING gin (chat_id, content_tsv)
  WHERE deleted_at IS NULL AND purged_at IS NULL AND sender_id IS NOT NULL;
```

A deployed DB needs `python3 -m scripts.init_db` + `scripts/backfill_search_tsv.py`
(per-partition batched `UPDATE ... WHERE content_tsv IS NULL`) run once by hand
(`deploy/README.md`). At real scale: `CREATE INDEX CONCURRENTLY` per partition +
`ALTER INDEX ... ATTACH PARTITION`.

## Data migration

PoC / dev only — the dev DB is re-initialised (`scripts/init_db.py`) and
reseeded; the trigger backfills on insert, the backfill script covers pre-existing
rows. No production data.

## Consequences

- **Write amplification**: every message INSERT / content-edit now also
  maintains `content_tsv` and the GIN. `fastupdate=on` (default) batches the
  index writes; autovacuum flushes the pending list. Accepted.
- **Storage**: one GIN over message text (~15–30% of text volume), partitioned.
- A partition **detached** by `manage_partitions.py --cold` drops out of search
  while detached (still searchable while merely frozen/attached).
- Search sees plaintext only because ADR 0039 removed E2EE — "secret chats"
  would be unsearchable by construction; none are planned.
- Opens the door to a future ADR for semantic / vector search (pgvector) over
  the same column.

## Deferred

- `pg_trgm` infix-substring / fuzzy search (config-flagged OR branch).
- `ts_rank` relevance ordering as a user-selectable sort.
- Semantic search / embeddings (pgvector) — separate ADR.
- PoC search UI — separate frontend task.
