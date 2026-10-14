"""Orchestration for semantic vector search (ADR 0042): enqueue on send,
flush-on-demand (size trigger + on-demand trigger), and the search itself.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.connection import session_scope
from modules.vector_search import crud, dev_cache, gemini_client, query_cache, queue
from modules.vector_search.errors import (
    EmbeddingProviderError,
    EmbeddingProviderQuotaExceededError,
    VectorSearchQueryTooShortError,
)
from modules.vector_search.limits import DEFAULT_VECTOR_SEARCH_LIMITS, VectorSearchLimits
from modules.vector_search.schemas import SemanticSearchResponseOut, SemanticSearchResultOut

logger = logging.getLogger(__name__)

# Only real text messages carry meaningful content to embed.
_TEXT_MESSAGE_TYPE = 1


async def enqueue_message_for_embedding(
    message_id: int,
    content: Optional[str],
    message_type: int,
    *,
    limits: VectorSearchLimits = DEFAULT_VECTOR_SEARCH_LIMITS,
) -> None:
    """Called from the send path right after the message is persisted - never
    awaited by the caller past the RPUSH itself. Fires a background flush
    (not awaited here either) once the queue crosses the size threshold, so
    the sender's request is never held up by a Gemini call."""
    if message_type != _TEXT_MESSAGE_TYPE or not content:
        return
    length = await queue.enqueue(message_id, content)
    if length >= limits.queue_flush_size:
        asyncio.create_task(_flush_and_log(limits))


async def _flush_and_log(limits: VectorSearchLimits) -> None:
    try:
        await flush_queue(limits=limits)
    except Exception:
        logger.exception("background vector-embed queue flush failed")


def _embed_fn():
    return dev_cache.embed_batch_cached if dev_cache.CACHE_ENABLED else gemini_client.embed_batch


def _bump_or_drop(items: list[dict], limits: VectorSearchLimits) -> list[dict]:
    """attempts + 1; items reaching the cap are dropped (id only in the log)."""
    keep = []
    for item in items:
        attempts = item.get("attempts", 0) + 1
        if attempts >= limits.embed_max_attempts:
            logger.warning("vector embed: dropping message %s after %d attempts", item["id"], attempts)
            continue
        keep.append({**item, "attempts": attempts})
    return keep


async def _embed_chunk(chunk: list[dict], limits: VectorSearchLimits) -> tuple[list[tuple[int, list[float]]], list[dict]]:
    """Embed one chunk. Returns (rows to write, items to requeue) - ADR 0107."""
    try:
        vectors = await _embed_fn()([c["content"] for c in chunk])
    except EmbeddingProviderQuotaExceededError:
        # Not the messages' fault: pause flushing, keep attempts unchanged.
        await queue.set_backoff(limits.backoff_seconds)
        logger.warning("vector embed: Gemini 429 - backing off %ds, requeueing %d", limits.backoff_seconds, len(chunk))
        return [], chunk
    except EmbeddingProviderError as exc:
        if not exc.is_client_error:
            logger.warning("vector embed: transient failure, requeueing %d", len(chunk))
            return [], _bump_or_drop(chunk, limits)
        if len(chunk) == 1:
            logger.warning("vector embed: dropping message %s (non-retriable %s)", chunk[0]["id"], exc.status_code)
            return [], []
        mid = len(chunk) // 2
        rows_l, requeue_l = await _embed_chunk(chunk[:mid], limits)
        if await queue.backoff_active():
            return rows_l, requeue_l + chunk[mid:]
        rows_r, requeue_r = await _embed_chunk(chunk[mid:], limits)
        return rows_l + rows_r, requeue_l + requeue_r
    except Exception:
        logger.exception("vector embed: unexpected failure, requeueing %d", len(chunk))
        return [], _bump_or_drop(chunk, limits)
    return [(int(c["id"]), vec) for c, vec in zip(chunk, vectors)], []


async def _process_items(items: list[dict], limits: VectorSearchLimits) -> int:
    embedded = 0
    to_requeue: list[dict] = []
    async with session_scope() as session:
        for start in range(0, len(items), limits.gemini_batch_size):
            chunk = items[start : start + limits.gemini_batch_size]
            rows, requeue_items = await _embed_chunk(chunk, limits)
            to_requeue.extend(requeue_items)
            if not rows:
                continue
            try:
                await crud.update_embeddings(session, rows)
                embedded += len(rows)
            except Exception:
                logger.exception("vector embed: DB write failed, requeueing %d", len(rows))
                done = {r[0] for r in rows}
                to_requeue.extend(_bump_or_drop([c for c in chunk if int(c["id"]) in done], limits))
    await queue.requeue(to_requeue)
    return embedded


async def flush_queue(*, limits: VectorSearchLimits = DEFAULT_VECTOR_SEARCH_LIMITS) -> int:
    """Pop up to `queue_flush_size` entries, embed them, write the vectors back.
    Returns the number of messages embedded. Failures are requeued (or dropped
    per ADR 0107) - never raised; a global 429 backoff makes this a no-op."""
    if await queue.backoff_active():
        return 0
    items = await queue.pop_batch(limits.queue_flush_size)
    if not items:
        return 0
    return await _process_items(items, limits)


async def flush_queue_if_pending(*, limits: VectorSearchLimits = DEFAULT_VECTOR_SEARCH_LIMITS) -> None:
    """On-demand trigger (ADR 0042 point 3), bounded per ADR 0107: at most
    `drain_rounds` rounds, each draining no more than the LLEN snapshot (so
    items requeued by a failure are not re-popped in the same round). After
    that, give up silently - the search runs without the newest messages and
    the leftovers are retried by the next flush. Never raises."""
    try:
        for _ in range(limits.drain_rounds):
            if await queue.backoff_active():
                return
            budget = await queue.queue_length()
            if budget <= 0:
                return
            while budget > 0:
                items = await queue.pop_batch(min(limits.queue_flush_size, budget))
                if not items:
                    break
                budget -= len(items)
                await _process_items(items, limits)
    except Exception:
        logger.exception("before-search vector queue drain failed")


async def semantic_search(
    session: AsyncSession,
    *,
    user_id: int,
    raw_query: str,
    chat_id: Optional[int],
    limit: int,
    expanded: bool = False,
    limits: VectorSearchLimits = DEFAULT_VECTOR_SEARCH_LIMITS,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    exclude_chat_ids: Optional[Sequence[int]] = None,
) -> SemanticSearchResponseOut:
    q = (raw_query or "").strip()
    if len(q) < limits.min_query_len:
        raise VectorSearchQueryTooShortError(f"query must be at least {limits.min_query_len} characters")
    q = q[: limits.max_query_len]
    if start_at is not None and end_at is not None and start_at > end_at:
        raise VectorSearchQueryTooShortError("start_at must not be after end_at")

    await flush_queue_if_pending(limits=limits)

    # ADR 0043's SQLite dev cache and ADR 0044's live in-memory LRU both wrap
    # embed_query - never stacked. Dev cache wins when explicitly enabled
    # (deterministic, unbounded, disk-backed); otherwise the LRU is always on,
    # prod included.
    embed_query_fn = dev_cache.embed_query_cached if dev_cache.CACHE_ENABLED else query_cache.embed_query_cached
    query_embedding = await embed_query_fn(q)
    max_distance = limits.max_distance_expanded if expanded else limits.max_distance
    rows = await crud.semantic_search_messages(
        session,
        user_id=user_id,
        query_embedding=query_embedding,
        limit=limit,
        max_distance=max_distance,
        chat_id=chat_id,
        start_at=start_at,
        end_at=end_at,
        exclude_chat_ids=exclude_chat_ids,
    )
    return SemanticSearchResponseOut(
        results=[
            SemanticSearchResultOut(
                id=r["id"],
                chat_id=r["chat_id"],
                sender_id=r["sender_id"],
                type=r["type"],
                content=r["content"],
                distance=r["distance"],
                created_at=r["created_at"],
            )
            for r in rows
        ]
    )
