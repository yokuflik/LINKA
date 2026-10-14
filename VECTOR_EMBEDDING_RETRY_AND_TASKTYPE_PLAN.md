# Vector embedding: retry queue, `taskType`, threshold recalibration

Status: **Parts 1 + 2 implemented 2026-10-15 (ADR 0107). Part 3 run 2026-10-15 (script + corpus + report); recommended thresholds NOT applied - see .claude_docs/vector_search.md.**
Extends ADR 0042 (semantic search) / ADR 0043 (dev embedding cache). Needs a new ADR (0107) before any code (Core Rule 7).

## Decisions already taken (owner)

- Paid Gemini tier: free-tier 100 req/min limit no longer applies; 429 is still handled, cheaply.
- Embedding failures must not leave messages without a vector: failed items go **back to the queue**.
- Search path must never hang on a broken embedder: bounded retries, then search proceeds without the newest messages.
- No poll loop / dedicated worker. Flush stays on-demand (size threshold + before-search).
- No backward compatibility for existing vectors/caches: all current data is junk. Change applies going forward only.
- Existing values already in the DB are not touched or re-embedded.

## Part 1 - Retry queue (`modules/vector_search/`)

### Queue item shape
`{"id": "<message_id>", "content": "...", "attempts": 0}`. Items without `attempts` (queued before deploy) read as 0.

### Config (`config/vector_settings.py` + `limits.py`)
| Name | Default | Meaning |
|---|---|---|
| `VECTOR_EMBED_MAX_ATTEMPTS` | 5 | per-item attempts before it is dropped (+ log) |
| `VECTOR_EMBED_BACKOFF_SECONDS` | 30 | global pause after a 429 |
| `VECTOR_SEARCH_DRAIN_ROUNDS` | 3 | drain attempts before a search gives up |

### `queue.py`
- `enqueue(message_id, content, attempts=0)`.
- `requeue(items)` - `RPUSH` items back (already carrying updated `attempts`).
- Backoff = **one Redis key** `vector_embed_backoff_until` (SET with EX). Cheapest option: no per-item timestamps, no ZSET.
  `backoff_active()` is one `EXISTS`.

### `gemini_client.py`
- `embed_batch` also raises `EmbeddingProviderQuotaExceededError` on 429 (today only `embed_query` does) and exposes the
  HTTP status class on the error so callers can tell 4xx (content) from 5xx/timeout (transient).

### `service.flush_queue` (normal / background mode, size >= 50)
1. If `backoff_active()` -> return 0 (items stay queued).
2. `pop_batch(queue_flush_size)`; chunk by `gemini_batch_size`.
3. Per chunk, `_embed_chunk`:
   - success -> `update_embeddings`.
   - **429** -> set backoff key, requeue the whole chunk, **attempts unchanged** (not the message's fault).
   - **other 4xx** (bad content) -> **bisect**: embed each half separately, recurse. A single item that still 4xx's is
     non-retriable -> drop immediately + log (retrying cannot heal it).
   - **5xx / timeout / network** -> no bisect (would double calls during an outage); requeue the whole chunk with
     `attempts + 1`.
4. Any item reaching `attempts >= VECTOR_EMBED_MAX_ATTEMPTS` is dropped with a warning log (id only, no content).

### `service.flush_queue_if_pending` (before-search mode)
- Rounds <= `VECTOR_SEARCH_DRAIN_ROUNDS` (3). Each round snapshots `LLEN` and drains at most that many items, so items
  requeued by a failure are **not** re-popped in the same round (no spin).
- Stop early when a round embeds everything / queue empty.
- After 3 rounds with leftovers (or backoff active): **give up silently**, search runs without the newest messages.
  Failed items are still requeued with `attempts + 1`, so they are retried by the next flush.
- Never raises into the search request.

### Known limits (documented, accepted)
- `LPOP` then process crash loses the popped chunk (no processing-list / consumer group). Out of scope.
- If nobody searches and < 50 new messages arrive, requeued items wait. Accepted (no poll loop).

## Part 2 - `taskType`

- `embed_batch(texts, task_type="RETRIEVAL_DOCUMENT")` - field set on **every object in `requests`**.
- `embed_query(text, task_type="RETRIEVAL_QUERY")` - field set in the body root of `embedContent`.
- Callers: `flush_queue`, `scripts/seed_vector_data.py`, `knowledge_service.py` (chunks = documents, query = query)
  all get the right default with no per-call change; verify each during implementation.
- Caches: `dev_cache._cache_key` and `query_cache._cache_key` include `task_type` (old SQLite cache entries become
  orphans, harmless; optionally `clear_cache()` once).
- No re-embed of existing rows. Stale IVFFlat centroids: optional `scripts.reindex_vectors` (centroid refresh only) once
  enough new vectors exist.

## Part 3 - Threshold recalibration (research, read-only)

Why: `VECTOR_SEARCH_MAX_DISTANCE=0.35` / `_EXPANDED=0.42` were tuned without `taskType`; the distance distribution
shifts. Must also hold for non-English (Hebrew first) queries.

### Tooling
- New `scripts/calibrate_vector_thresholds.py`. **Read-only: never touches Postgres or Redis.** Real Gemini calls with
  the owner's key (`GEMINI_API_KEY` from `.env`), same model/dim (768) as production. Results cached in a local JSON
  file under the scratchpad so reruns cost nothing. Output: a Markdown report + JSON, nothing committed to the DB.
- Distance computed as `1 - cosine` on the 768-d truncated vectors (matches pgvector `<=>` with `vector_cosine_ops`).

### Corpus (written by me, owner reviews before any API spend)
- Languages (editable): Hebrew (primary), English, Arabic, Russian, Spanish, French.
- ~40 topics x 3 chat-style documents (5-20 words, informal, typos/slang included) per language -> ~600 docs.
- Per topic per language: 2 queries - one 1-3 word keyword query (how people actually search), one natural question.
- Cross-language slice: Hebrew query vs English doc and vice versa (multilingual chats).
- Graded labels, authored by me per (query, doc):
  - **2** clearly relevant (should pass the strict threshold)
  - **1** loosely related / same domain (should pass only the expanded threshold)
  - **0** irrelevant, including **hard negatives** (shared words, different meaning; same topic, different intent).
- Estimated ~1,100 embedding inputs total (one-off, cents).

### Method
1. Embed docs as `RETRIEVAL_DOCUMENT`, queries as `RETRIEVAL_QUERY`.
2. Per language and pooled: distance histograms per label, ROC / PR.
3. **Strict** (`VECTOR_SEARCH_MAX_DISTANCE`): label 2 vs {1,0}; pick the threshold at precision >= 0.9 with best recall.
4. **Expanded** (`..._EXPANDED`): {2,1} vs 0; pick the threshold at recall >= 0.9 with best precision.
5. Report per-language optimum and the spread; pick one global value unless the spread is large (then flag per-language
   skew, e.g. Hebrew distances systematically higher).
6. Optional baseline: same corpus without `taskType`, to quantify the shift from the old 0.35 / 0.42.
7. Deliverable: recommended two numbers + report. Applying them (env / config default) is a separate owner decision.

### Caveats to state in the report
- Synthetic corpus, not real chat distribution; re-check once real Hebrew chat data exists.
- Short messages dominate chat; embeddings of 1-3 word queries are noisy, so margins matter more than the point optimum.

## Implementation order (each gated on approval)

1. ADR 0107 (retry queue + `taskType` + recalibration decision) and CLAUDE.md ADR-index row.
2. `taskType` + cache keys (smallest, independent) + tests.
3. Retry queue (`queue.py`, `service.py`, `gemini_client.py` error typing, config/limits) + tests.
4. Calibration script + corpus (corpus reviewed by owner first), run, report.
5. Update `.claude_docs/` (vector search coverage; create a `vector_search.md` sub-file if none exists) and set agreed
   thresholds.

## Tests (`tests/modules/vector_search/`)
- `taskType` present in batch body (per request) and in query body; cache keys differ by task type.
- Requeue on 5xx increments `attempts`; drop at 5; 429 sets backoff and does not consume an attempt; backoff skips flush.
- 4xx bisect isolates one poisoned item among N; the rest are embedded.
- Before-search drain: 3 rounds max, never raises, no same-round re-pop; search succeeds with leftovers.
- Legacy queue item without `attempts` handled.
- Warning: tests `flushdb` the dev Redis (6380/0) - do not run while `run_dev.sh` is up.
