# Semantic vector search (ADR 0042 / 0043 / 0044 / 0107)

`modules/vector_search/`: `gemini_client.py` (raw httpx, `gemini-embedding-001`, 768-d), `queue.py` (Redis LIST), `service.py` (enqueue / flush / search), `crud.py`, `dev_cache.py` (SQLite, `VECTOR_EMBED_CACHE=1`), `query_cache.py` (live TTL LRU), `limits.py`, `errors.py`, `router.py`.

## Embedding flow
- Send path -> `enqueue_message_for_embedding` (type-1 text only) -> `RPUSH` `{"id","content","attempts"}` (missing `attempts` = 0). Queue >= `VECTOR_QUEUE_FLUSH_SIZE` (50) fires a background `flush_queue`.
- `taskType`: `embed_batch` = `RETRIEVAL_DOCUMENT` (set on every request in the batch), `embed_query` = `RETRIEVAL_QUERY`. Both caches key on task type.

## Retry queue (ADR 0107)
- `flush_queue`: no-op while Redis key `vector_embed_backoff_until` exists. Per chunk (`_embed_chunk`):
  - 429 (`EmbeddingProviderQuotaExceededError`, now also from `embed_batch`) -> set backoff key (`VECTOR_EMBED_BACKOFF_SECONDS`=30), requeue, attempts unchanged.
  - other 4xx (`EmbeddingProviderError.is_client_error`) -> bisect; single failing item dropped + logged.
  - 5xx / timeout / unexpected -> requeue whole chunk, `attempts+1`; `>= VECTOR_EMBED_MAX_ATTEMPTS` (5) -> dropped (id-only warning).
  - DB write failure -> requeue those items with `attempts+1`.
- `flush_queue_if_pending` (before every semantic search): <= `VECTOR_SEARCH_DRAIN_ROUNDS` (3) rounds, each draining at most the `LLEN` snapshot; stops on backoff; never raises; search proceeds without the newest messages.
- Known gaps: `LPOP` then crash loses the popped chunk; leftovers wait if < 50 messages arrive and nobody searches (no poll loop).
- Callers of `embed_batch` with defaults: `service.flush_queue`, `scripts/seed_vector_data.py`, `modules/agents/knowledge_service.py` (now also catches the quota error).

## Thresholds (calibrated 2026-10-15; strict APPLIED = 0.32, expanded unchanged 0.42)
`scripts/calibrate_vector_thresholds.py` (read-only; corpus in `scripts/calibration_corpus/`: 6 langs x 30 topics x 3 docs + 4 confusers, 360 queries; `--baseline` also runs without taskType; cache `.dev_cache/calibration_embeddings.json`). Results, with taskType, pooled all-pairs:
- Strict (label 2 vs rest): precision 0.91 at **0.32** (recall 0.40); current 0.35 gives precision 0.78 / recall 0.63. Same-language optimum ~0.295, cross-language ~0.34; per-language spread tiny (0.29-0.31), Hebrew not skewed.
- Expanded: "same-domain" (label 1) overlaps unrelated (label 0) heavily (medians 0.416 vs 0.436), so recall>=0.9 costs precision ~0.18 - not usable as a target. Cross he<->en relevant p95 = 0.408, so the current 0.42 still covers ~95% of cross-lingual matches.
- Finding: taskType did NOT improve separation on this corpus (label-0 distances shrank 0.460->0.436 median, same-language strict recall 0.55 vs 0.58 baseline). Retest on real chat data before relying on it.
Caveats: synthetic corpus of 564 docs; precision is corpus-size dependent. Strict lowered 0.35 -> 0.32 by owner decision.
