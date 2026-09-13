# ADR 0042 — Semantic vector search via Gemini embeddings + pgvector IVFFlat

Status: Accepted
Date: 2026-09-11

Enabled by: ADR 0039 (`messages.content` is server-readable plaintext) and
ADR 0040 (keyword FTS already covers exact/prefix matching — this ADR adds
*meaning*-based recall alongside it, not instead of it).

## Context

We want semantic ("meaning") search over message text, on top of the existing
keyword FTS (ADR 0040), demonstrable in technical interviews. Constraints:

- Single 1 GB t3.micro demo host (ADR 0007) — an HNSW pgvector index keeps its
  whole graph in RAM at build time and is known to OOM-kill on hosts this
  small once the row count is even modest. **Out.**
- A pure sequential scan (`ORDER BY embedding <=> :q LIMIT k` with no index) is
  O(N) per query — unacceptable to present as the search path at "billions of
  messages" scale, even though the demo dataset is tiny.
- No paid embedding API budget — Google's free-tier Gemini `gemini-embedding-001`
  (Matryoshka-truncated to 768-dim via `outputDimensionality`) is the only
  embedding source, capped at **100 embed sub-requests/minute** (each item
  inside a `batchEmbedContents` call counts individually against that quota),
  imposing app-level batching discipline.
- No DB migrations (CLAUDE.md) — schema changes go through `scripts/init_db.py`
  with `IF NOT EXISTS`.
- Sending a message must stay instant — embedding generation cannot sit on the
  hot send path (already async off `message_send_stream`, ADR 0037-style).

## Decision

### 1. Index: IVFFlat, not HNSW

- `messages.embedding vector(768)` (pgvector), nullable.
- `CREATE INDEX ... USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)`.
  IVFFlat's memory footprint is a small multiple of the vector data itself
  (no in-RAM graph like HNSW) — safe on a 1 GB host. Recall is lower than
  HNSW's, an accepted trade for staying inside the memory budget.
- **IVFFlat requires representative data to compute its cluster centroids** —
  building it on an empty/near-empty table gives useless centroids. The index
  is therefore **not** created by `init_db.py`. `modules/vector_search/ddl.py`
  only adds the extension + column; a dedicated helper
  (`ensure_ivfflat_index`) is called explicitly by the seed script **after**
  the backfill completes, and can be re-run later (`REINDEX`) once real
  production volume exists.
- `lists = 100` is the pgvector-recommended starting point for a dataset in
  the low-to-mid thousands of rows (`rows / 1000`, floored at reasonable
  bounds); revisit if the corpus grows by orders of magnitude.

### 2. Embedding provider: Gemini `gemini-embedding-001`, called over raw HTTPS

- `modules/vector_search/gemini_client.py` calls
  `generativelanguage.googleapis.com` directly via `httpx` (already a project
  dependency) — no new Google SDK, consistent with the project's existing
  manual-HTTP style for third-party verification (`modules/auth/firebase.py`).
- `batchEmbedContents` — one HTTP call, `VECTOR_GEMINI_BATCH_SIZE` (90, not
  100 — margin against the per-minute quota, see above) texts per call, each
  counted as its own request against the 100/minute ceiling.
- **Seed script** (`scripts/seed_vector_data.py`): embeds whatever un-embedded
  text messages `scripts/seed_mock_data.py` already inserted (default sizing:
  3 users, 4 chats, 1,000 messages total) → 12 requests of ≤90 messages each,
  with a full `time.sleep(65)` between requests (the quota window is a
  *minute*, not a few seconds — an earlier `time.sleep(4)` design, sized for
  the older `text-embedding-004` model's since-retired 15 RPM limit, 429'd
  immediately once the model was swapped for `gemini-embedding-001`). Run
  once, offline, not on any request path. It does not insert a second batch
  of messages — one seeded corpus, embedded in place.
- **Query embedding** (search time): one `embedContent` call per search
  request — well inside the per-minute budget at demo traffic.

### 3. Flush-on-demand queue — embeddings off the send hot path

- `send_message` already persists the `Message` row and returns before
  fan-out completes (async send path). After `create_message` succeeds for a
  **text** message, `modules/vector_search/service.enqueue_message_for_embedding`
  `RPUSH`es `{message_id, content}` onto a Redis list
  (`vector_embed_queue`) — O(1), no external call, no added latency.
- **Trigger 1 — size**: after every push, if `LLEN >= VECTOR_QUEUE_FLUSH_SIZE`
  (50), a flush is kicked off as a background task (`asyncio.create_task`) —
  never awaited inline on the sender's request.
- **Trigger 2 — on-demand**: `modules/vector_search/router.py`'s semantic
  search endpoint calls `service.flush_queue_if_pending` **synchronously**
  before running the DB query, so a message sent seconds ago is guaranteed
  searchable even if the 50-message threshold hasn't been hit yet.
- A flush pops up to `VECTOR_QUEUE_FLUSH_SIZE` entries (`LPOP` count), calls
  Gemini's `batchEmbedContents` once, and `UPDATE`s `messages.embedding` per
  row by id. A failed flush (Gemini error) drops that batch rather than
  requeuing — acceptable for a demo feature; those messages simply won't
  surface in semantic search until edited/resent. No retry queue (kept out of
  scope, flagged as a known gap below).
- Only **text** messages (`type == 1`) with non-empty `content` are queued.

### 4. Query execution: cosine similarity, membership enforced in-query

- `SELECT ... FROM messages m JOIN participants p ON p.chat_id = m.chat_id AND
  p.user_id = :user_id WHERE m.embedding IS NOT NULL ... ORDER BY m.embedding
  <=> :query_vector LIMIT :k` — the same "membership via INNER JOIN, not a
  pre-fetched chat-id list" pattern ADR 0040 established, so a removed member
  loses access immediately, not at the next cache refresh.
- `<=>` (cosine distance) matches the index's `vector_cosine_ops` opclass —
  required for the planner to use the IVFFlat index at all.
- Soft-deleted / purged / system rows excluded, same partial predicate style
  as the FTS index (enforced in the query; the IVFFlat index itself is not
  partial — pgvector does not support a `WHERE` clause referencing another
  column efficiently in this version, so filtering stays in the query, same
  as `messages_around` does for tombstones).

## Consequences

- New dependency: `pgvector` (Python) for the SQLAlchemy `Vector` type, and
  the Postgres `vector` extension (`CREATE EXTENSION IF NOT EXISTS vector`).
  **The extension binary itself must be present on the Postgres server** —
  plain `postgres:15-alpine` does not ship it. Both `docker-compose.yml`
  (`test_db`) and `docker-compose.prod.yml` (`db`) switch to
  `pgvector/pgvector:pg15` — same Postgres 15 data format (compatible with an
  existing `pgdata` volume, no dump/restore needed), extension included. A
  local dev DB created before this change needs its container recreated with
  the new image (`docker compose down && docker compose up -d`) before
  `CREATE EXTENSION IF NOT EXISTS vector` succeeds.
- New env var `GEMINI_API_KEY` (empty → semantic search endpoint returns a
  clean 503, never a stack trace — mirrors `FIREBASE_PROJECT_ID`'s
  empty-disables-feature pattern).
- Recall is approximate (IVFFlat, not exact kNN) — acceptable trade for the
  memory budget; documented, not hidden.
- **Known gap, deliberately deferred**: no retry/dead-letter path for a failed
  Gemini flush batch; no re-embedding on message edit (edited text keeps its
  original — or absent — embedding until a future backfill pass); IVFFlat
  `lists` is not re-tuned automatically as the table grows.
