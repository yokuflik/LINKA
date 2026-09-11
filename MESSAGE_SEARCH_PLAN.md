# Message Search — Design & Delivery Plan

Server-side keyword search over message text. Two surfaces: **in-chat** (one
`chat_id`) and **global** (every chat the caller is a member of). Strict
query-level permission enforcement, Postgres-only indexing (no Elasticsearch),
an SSE stream for huge global result sets, a cursor-paginated endpoint for
jump-to-context, and Redis rate limiting on every search endpoint.

Prereq: **ADR 0040** (Postgres FTS for message search) must land before code
(Core Rule 7). This file is the implementation plan; the ADR carries the
rationale. Also needs user sign-off (Rule 10 — backend + schema change).

Depends on **ADR 0039** — E2EE was dropped, `messages.content` is now plaintext
the server can read. That is the enabling decision for search.

---

## 1. Indexing strategy — recommendation

**Native PostgreSQL Full-Text Search (`tsvector` + GIN), `'simple'` config.**
Not `pg_trgm` as the primary, not an external service.

Why FTS over trigram as primary:

| | FTS (`tsvector`/GIN) | `pg_trgm` GIN |
|---|---|---|
| Index size (billions of rows) | ~15–30% of text volume | ~100–200% of text volume — infeasible on the target box |
| Word / prefix ("as-you-type") match | native (`foo:*`) | works but slower, scans more |
| Phrase, AND/OR, `-exclude` | `websearch_to_tsquery` | manual |
| Ranking | `ts_rank` | `similarity()` |
| Infix substring (`cat` in `concatenate`) | **no** | yes |
| Typo tolerance | no | yes (`similarity`) |
| Write amplification on `messages` INSERT | one GIN | one larger GIN |

For a WhatsApp-style "search messages" box, word + prefix match is the correct
product behaviour and FTS is dramatically cheaper. `'simple'` dictionary (no
stemming, no stop-word removal) is deliberate: chat is multilingual, so
language-specific stemming would corrupt more than it helps, and "to be or not
to be" must stay searchable.

**`pg_trgm` is deferred**, not rejected: if infix/fuzzy search becomes a
requirement, add a second `gin (content gin_trgm_ops)` index behind a config
flag and an OR branch in the query builder. Not in scope now — a second
billion-row GIN is the exact cost we are avoiding.

### Column & trigger

- `messages.content_tsv tsvector` — nullable, added via
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `init_db.py` (no migrations).
- Maintained by a **row trigger on the partitioned parent** (propagates to all
  current and future partitions), `BEFORE INSERT OR UPDATE OF content, media_name`:
  `NEW.content_tsv := to_tsvector('simple', coalesce(NEW.content,'') || ' ' || coalesce(NEW.media_name,''))`.
  Trigger chosen over a `GENERATED` column because adding a stored generated
  column rewrites every partition and locks; a `CREATE OR REPLACE FUNCTION` +
  `CREATE TRIGGER` is idempotent-friendly and covers edit/restore/purge for free
  (`purge_message` nulls `content` → trigger clears the vector).
- Media captions are included (they live in `content`); `media_name` is folded
  in so a filename is findable. System messages are excluded at index time via
  the partial index predicate below.

### Index

One composite GIN via the **`btree_gin`** extension:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE INDEX IF NOT EXISTS ix_messages_chatid_tsv
  ON messages USING gin (chat_id, content_tsv)
  WHERE deleted_at IS NULL AND purged_at IS NULL AND sender_id IS NOT NULL;
```

- Serves **in-chat** search directly (`chat_id = :cid AND content_tsv @@ :q`) —
  `btree_gin` makes `chat_id` an indexable GIN key so there is no post-filter
  scan across other chats.
- Serves **global** search too (`content_tsv @@ :q` alone — GIN does not require
  a leading constant like a btree).
- Partial predicate keeps deleted / purged / system rows out of the index
  entirely; every search query carries the same three predicates so the index is
  always usable.
- Created on the parent → PG15 propagates to each partition; new weekly
  partitions from `manage_partitions.py` inherit it automatically via
  `CREATE TABLE ... PARTITION OF` (confirm no explicit index list there needs the
  entry added).

### Backfill (existing rows)

`scripts/backfill_search_tsv.py` — per-partition, batched
`UPDATE ... WHERE content_tsv IS NULL AND ctid = ANY(...)` loops, committing each
batch. Dev/demo data is tiny (seed = 5 users) so this is instant there;
documented for the deployed DB. For a real large DB: build the GIN per partition
with `CREATE INDEX CONCURRENTLY` on the partition then `ALTER INDEX ... ATTACH
PARTITION`.

---

## 2. Permission enforcement — at the query level

**In-chat** (`GET /chats/{chat_id}/messages/search`):
`is_participant(session, chat_id, user_id)` → `NotAParticipantError` / 403
before the query; then `WHERE chat_id = :chat_id`.

**Global** (`GET /search/messages`, `GET /search/messages/stream`): the query
**joins `participants`** so membership is enforced inside the SQL, not by a
pre-filter:

```sql
SELECT m.*
FROM messages m
JOIN participants p ON p.chat_id = m.chat_id AND p.user_id = :user_id
WHERE m.content_tsv @@ :tsquery
  AND m.deleted_at IS NULL AND m.purged_at IS NULL AND m.sender_id IS NOT NULL
  AND m.id < :cursor_id                         -- when paging
ORDER BY m.id DESC
LIMIT :limit + 1
```

- Every returned row is provably in a chat the user is a **current** member of.
  A user removed from a group immediately stops seeing its messages in search —
  a pre-fetched `chat_id` list (point-in-time snapshot) would not guarantee that.
- The `p` join is a PK hit on `participants (chat_id, user_id)` — O(log n) per
  candidate row.
- Optimisation for users in few chats: when `get_all_chat_ids_for_user` returns
  `< SEARCH_ANY_INLINE_MAX` (≈50) ids, also pass `AND m.chat_id = ANY(:ids)` as a
  planner hint (still `btree_gin`-indexable). Above that, rely on the join.
- History is **not** clipped on join anywhere else in the app (members see full
  history), so search needs no join-date floor — consistent with
  `get_chat_messages`.

No masking logic applies (read-receipt privacy, mute, etc. are irrelevant to
search). Encrypted content: N/A post-ADR-0039.

---

## 3. Query-string → `tsquery`

`modules/search/service.py::build_tsquery(raw: str) -> str | None`:

1. Trim, collapse whitespace, NFC-normalise. Reject `len < SEARCH_MIN_QUERY_LEN`
   (2) → `SearchQueryTooShortError` → **422, no DB hit** (kills as-you-type spam
   at the cheapest point).
2. `websearch_to_tsquery('simple', raw)` — gives the user phrase (`"..."`),
   implicit AND, `or`, and `-term` exclusion.
3. Prefix: append `:*` to the trailing bare lexeme for as-you-type
   (`hello wor` → `hello & wor:*`). Skip when the query ends in a quote or `-`.
4. Empty result (query was all stop-symbols) → `None` → 422.

Partition pruning: when a cursor is present, add
`created_at <= id_to_datetime(cursor_id) + _PARTITION_SKEW` exactly as
`crud_message.get_chat_messages` does, so Postgres skips partitions newer than
the cursor. First page (no cursor) hits every partition's GIN slice — bounded by
`LIMIT` + `statement_timeout`.

Every search query runs with `SET LOCAL statement_timeout = SEARCH_STATEMENT_TIMEOUT_MS`
(3000) so one pathological scan cannot peg the single CPU.

---

## 4. Endpoints

New feature module `modules/search/` (ADR 0022 layout):
`schemas.py` · `crud.py` · `service.py` · `router.py` · `limits.py` · `errors.py`.
Routers registered in `main.py`. Error→HTTP mapping in `main.py`.

### 4a. In-chat — `GET /chats/{chat_id}/messages/search`
Query: `q`, `before_id?` (cursor), `limit` (clamp `[1, 50]`).
→ `SearchResponseOut { results: [SearchResultOut], next_cursor: str | None, has_more: bool }`.
`SearchResultOut` = `MessageOut` shape + `snippet`. Ordered `id DESC`.

### 4b. Global cursor — `GET /search/messages`
Query: `q`, `cursor?` (opaque base64 of last `id`), `limit` (clamp `[1, 50]`),
optional `chat_id` (falls through to the same code as 4a).
→ `SearchResponseOut`. Each result carries `chat_id` (+ `chat_title` / peer name
resolved once per distinct chat) so the client can open that chat.
This is the **default** endpoint. Ordered `id DESC` (recency, not `ts_rank` —
matches chat-search UX and streams straight off the index).

### 4c. Global stream — `GET /search/messages/stream`
`text/event-stream` via Starlette `StreamingResponse` + an async generator over a
**server-side cursor** (`session.stream()` / `.stream_scalars()`, batch
`SEARCH_STREAM_BATCH` = 100). For a global search that matches millions of rows
across huge chats, this avoids building a giant list / one huge JSON body in the
single app process.

- Frames: `event: match\ndata: {SearchResultOut}\n\n` per row;
  `event: done\ndata: {"count": N, "truncated": bool}\n\n` at the end;
  `event: error\ndata: {"detail": ...}\n\n` on failure.
- `: keepalive\n\n` every `SEARCH_STREAM_KEEPALIVE_S` (15).
- Hard caps: `SEARCH_STREAM_MAX_RESULTS` (500) rows **and**
  `SEARCH_STREAM_MAX_SECONDS` (20) wall-clock — either hit → `done` with
  `truncated: true`. Keeps the read transaction (and its cursor) short-lived on
  t3.micro.
- Backpressure is automatic: the generator awaits the ASGI send, so a slow
  client throttles the PG cursor.
- Permission JOIN is in the streamed query; membership is checked at query time
  only — a mid-stream removal is not reflected (stream is seconds-long;
  documented).
- Caddy: the `/search/*` proxy path needs `flush_interval -1` (disable response
  buffering) — add a matcher in `deploy/Caddyfile`.

### 4d. Jump-to-context — `GET /chats/{chat_id}/messages/around/{message_id}`
Query: `radius` (clamp `[1, 50]`, default 25).
→ `radius` messages before + the target + `radius` after, one round trip.
Needed because the existing history endpoint paginates **backwards only**;
"jump to this search result in context" needs messages after it too.
`is_participant` gate; two indexed `(chat_id, id)` range scans (`id <=` desc
limit, `id >` asc limit) with the usual `created_at` skew predicate; merge.

### Snippet
Computed **app-side** in `service.py` (find first match offset in `content`,
slice ±`SEARCH_SNIPPET_RADIUS` chars, ellipsise) — cheaper than SQL
`ts_headline` (which reparses the document) on a 1-CPU box. `ts_headline` noted
as the alternative if highlight fidelity matters later.

### Payload / privacy
`SearchResultOut` reuses `MessageOut` (already carries `sender_id` for API logic;
the PoC resolves it to a name per Frontend Rule 5 — consistent). Media matches:
**no presigned `media_url`** in search results (kept light); `media_blur_hash`
is included, and the real bytes load when the user jumps to context via the
normal history path.

---

## 5. Rate limiting (Redis)

`modules/search/limits.py::SearchLimits` frozen dataclass + `get_search_limits`
FastAPI dep (ADR 0033 pattern). Knobs in a new `config/search_settings.py`
sub-module (ADR 0019), all `SEARCH_*`. Sliding-window via
`infra/ratelimit/service.py`; → HTTP 429 via the existing handler.

| Bucket | Key | Limit | Enforced |
|---|---|---|---|
| `search_query` (two-tier) | `rlsw:search_query:{uid}` + `rlsw:search_query_burst:{uid}` | 10 / 10 s **and** 30 / 60 s | 4a + 4b + 4d, before the query |
| `search_stream` | `rlsw:search_stream:{uid}` | 3 / 60 s | 4c, before opening the cursor |
| stream concurrency | `search:stream:active:{uid}` (SETNX + 30 s TTL, released on generator close) | 1 active stream / user | 4c → `SearchStreamBusyError` / 409 |
| `search_ip` | `ratelimit:search_ip:{ip}` (fixed) | 60 / 60 s | shared ceiling on all search routes |

Plus: server-enforced `SEARCH_MIN_QUERY_LEN` (422 before any DB work) and a
documented **client-side debounce ≥ 400 ms** for the as-you-type box (Frontend).
The global `_per_ip_backstop` (1000 / 180 s) already sits above all of this.

---

## 6. `scripts/init_db.py` additions (non-`--drop` path, no migrations)

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
run once by hand (add to `deploy/README.md`). `manage_partitions.py`: confirm
new partitions inherit the GIN (they do via `PARTITION OF`); no code change
expected.

---

## 7. Performance notes (t3.micro — 1 GB, burst CPU)

- **One** composite GIN, not two. `'simple'` config loads no dictionary files.
- No `ts_headline` in SQL; snippet is app-side.
- `statement_timeout` 3 s on every search query; stream capped at 500 rows / 20 s.
- Stream uses a server-side cursor, batch 100 — constant memory in the app proc.
- GIN `fastupdate=on` (default) batches index writes from the message INSERT hot
  path; autovacuum flushes the pending list. Keep default; monitor
  `gin_pending_list_limit`.
- Write amplification: every message INSERT/edit now also maintains `content_tsv`
  + the GIN. Acceptable, documented in the ADR.
- Old partitions moved to cold storage / detached by the existing
  `manage_partitions.py --cold` path stay searchable while attached; a *detached*
  partition drops out of search (note in ADR).

---

## 8. Delivery steps — DONE (2026-09-10)

All landed except the PoC search UI (separate frontend task) and `pg_trgm`
(deferred). Backend: `modules/search/{__init__,ddl,errors,limits,schemas,crud,
service,router}.py`, `config/search_settings.py` (wired into `config/_accessor.py`
+ `config/__init__.py`), `messages.content_tsv` column on the `Message` model,
`apply_search_ddl` called from `scripts/init_db.py` **and** `tests/conftest.py`,
`scripts/backfill_search_tsv.py`, `main.py` (2 routers + 2 exception handlers +
`_per_ip_backstop` stream skip), `deploy/Caddyfile` (`/search*` in `@api` + a
`handle /search/messages/stream` with `flush_interval -1`).
Tests: `tests/modules/search/test_search.py` (10) + `test_search_api.py` (8) —
match/prefix/phrase/`-exclude`, recency order, deleted/purged/system excluded,
non-participant 403, removed-member excluded (JOIN), cursor paging, 422, 429,
SSE `match`+`done`, stream 409, `around` window. **424 tests pass.**
Docs: `.claude_docs/search.md` (new) + `database_schema.md` +
`security_and_rate_limiting.md` + `backend_services_and_api.md` + CLAUDE.md
indexes + `deploy/README.md`.

---

## 9. Open product calls (decide in the ADR)

- Search `media_name` (filenames)? — **plan assumes yes** (folded into the vector).
- Search system messages? — **no** (partial-index predicate excludes them).
- Fuzzy / typo tolerance? — **no** now; `pg_trgm` is the documented later add.
- Rank vs recency ordering? — **recency** (`id DESC`).
- Search all history vs last N months? — **all attached partitions**.
- Global result ordering across chats — single `id DESC` merge (Snowflake ids are
  time-sortable), not per-chat grouping. Client can group by `chat_id` for display.
