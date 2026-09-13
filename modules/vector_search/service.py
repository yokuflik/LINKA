"""Orchestration for semantic vector search (ADR 0042): enqueue on send,
flush-on-demand (size trigger + on-demand trigger), and the search itself.
"""

import asyncio
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.connection import session_scope
from modules.vector_search import crud, dev_cache, gemini_client, query_cache, queue
from modules.vector_search.errors import VectorSearchQueryTooShortError
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


async def flush_queue(*, limits: VectorSearchLimits = DEFAULT_VECTOR_SEARCH_LIMITS) -> int:
    """Pop up to `queue_flush_size` entries, embed them in one Gemini batch
    call, write the vectors back. Returns the number of messages embedded.
    A Gemini failure drops this batch (see ADR 0042's known-gaps) rather than
    requeuing - callers (auto-flush / on-demand) both treat 0 as "nothing to
    report", not an error."""
    items = await queue.pop_batch(limits.queue_flush_size)
    if not items:
        return 0
    embedded = 0
    async with session_scope() as session:
        for start in range(0, len(items), limits.gemini_batch_size):
            chunk = items[start : start + limits.gemini_batch_size]
            try:
                embed_fn = dev_cache.embed_batch_cached if dev_cache.CACHE_ENABLED else gemini_client.embed_batch
                vectors = await embed_fn([c["content"] for c in chunk])
            except Exception:
                logger.exception("Gemini embed_batch failed - dropping %d queued messages", len(chunk))
                continue
            rows = [(int(c["id"]), vec) for c, vec in zip(chunk, vectors)]
            await crud.update_embeddings(session, rows)
            embedded += len(rows)
    return embedded


async def flush_queue_if_pending(*, limits: VectorSearchLimits = DEFAULT_VECTOR_SEARCH_LIMITS) -> None:
    """On-demand trigger (ADR 0042 point 3): called synchronously before a
    semantic search runs so a just-sent message is guaranteed searchable, even
    below the auto-flush size threshold. Keeps draining until the queue is
    empty (a single search request should not leave a partially-drained queue
    for the next one to redo)."""
    while await queue.queue_length() > 0:
        embedded = await flush_queue(limits=limits)
        if embedded == 0:
            # Either the queue emptied under us or every item's flush failed -
            # either way, looping again would spin forever.
            break


async def semantic_search(
    session: AsyncSession,
    *,
    user_id: int,
    raw_query: str,
    chat_id: Optional[int],
    limit: int,
    expanded: bool = False,
    limits: VectorSearchLimits = DEFAULT_VECTOR_SEARCH_LIMITS,
) -> SemanticSearchResponseOut:
    q = (raw_query or "").strip()
    if len(q) < limits.min_query_len:
        raise VectorSearchQueryTooShortError(f"query must be at least {limits.min_query_len} characters")
    q = q[: limits.max_query_len]

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
