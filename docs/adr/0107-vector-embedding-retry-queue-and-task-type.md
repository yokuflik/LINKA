# ADR 0107 — Vector embedding: retry queue, `taskType`, threshold recalibration

Status: Accepted
Date: 2026-10-15

## Context

ADR 0042's flush path drops a whole batch on any Gemini failure, so a transient
error (or a 429) leaves messages permanently without a vector. Embeddings are
also requested without `taskType`, so documents and queries are not embedded in
the asymmetric retrieval spaces the model offers, and the distance thresholds
(0.35 / 0.42) were tuned without it. The deploy now uses the paid Gemini tier.

## Decision

1. **Retry queue.** Queue entries gain `attempts` (missing = 0). Failed items go
   back via `RPUSH` (`queue.requeue`):
   - 429 → one Redis key `vector_embed_backoff_until` (`SET EX
     VECTOR_EMBED_BACKOFF_SECONDS`); chunk requeued, attempts unchanged; flush
     is skipped while the key exists.
   - other 4xx → bisect the chunk; a single item that still 4xx's is dropped
     immediately (retry cannot heal it).
   - 5xx / timeout / network → requeue the whole chunk with `attempts + 1`
     (no bisect — would double calls during an outage).
   - `attempts >= VECTOR_EMBED_MAX_ATTEMPTS` (5) → dropped with a warning log
     (id only).
2. **Search never hangs.** `flush_queue_if_pending` runs at most
   `VECTOR_SEARCH_DRAIN_ROUNDS` (3) rounds, each draining at most the `LLEN`
   snapshot (requeued items are not re-popped in-round), never raises; search
   proceeds without the newest messages.
3. **`taskType`.** `embed_batch` sends `RETRIEVAL_DOCUMENT` on every request;
   `embed_query` sends `RETRIEVAL_QUERY`. Both cache keys include the task type.
4. **No backward compatibility.** Existing vectors/caches are not re-embedded;
   the change applies going forward only.
5. **Threshold recalibration** is a separate read-only script
   (`scripts/calibrate_vector_thresholds.py`); applying new numbers is a
   separate owner decision.

No poll loop / dedicated worker: flush stays on-demand (size threshold +
before-search). Accepted gaps: `LPOP` then process crash loses the popped
chunk; requeued items wait if < 50 messages arrive and nobody searches.
