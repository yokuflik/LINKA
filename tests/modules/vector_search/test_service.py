"""service coverage for semantic vector search (ADR 0042): query validation,
the dev-cache/live-cache embed switch, flush_queue batching + drop-on-failure,
flush_queue_if_pending's drain loop, and enqueue_message_for_embedding's
type/content gate + auto-flush trigger.

Gemini itself is monkeypatched throughout (this module is the orchestration
layer, not the HTTP client) but the DB/Redis it drives are real, matching the
project's convention (see modules/search/test_search.py).
"""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging.crud import create_message
from modules.vector_search import dev_cache, gemini_client, query_cache, queue, service
from modules.vector_search.errors import VectorSearchQueryTooShortError
from modules.vector_search.limits import VectorSearchLimits
from infra.ids.snowflake import next_id

from tests.modules.vector_search._vectors import unit_vector

pytestmark = pytest.mark.asyncio

_MID_BASE = next_id() & ~0x3FFFFF


def _mid(n: int) -> int:
    return _MID_BASE + n


_LIMITS = VectorSearchLimits(
    min_query_len=2,
    max_query_len=20,
    default_limit=10,
    max_limit=30,
    max_distance=1.5,
    max_distance_expanded=1.9,
    rate_max=10,
    rate_window_s=60,
    queue_flush_size=3,
    gemini_batch_size=2,
)


async def _user(session, uid):
    from modules.users.crud import create_user

    await create_user(session, user_id=uid, phone_number=f"+97270{uid}")


async def _chat(session, chat_id, *user_ids):
    await create_chat(session, chat_id=chat_id, is_group=True, title=f"c{chat_id}")
    for uid in user_ids:
        await add_participant_to_chat(session, chat_id=chat_id, user_id=uid)


@pytest.fixture(autouse=True)
def _reset_query_cache():
    from cachetools import TTLCache

    query_cache._cache = TTLCache(maxsize=query_cache._MAXSIZE, ttl=query_cache._TTL_SECONDS)
    query_cache.hits = 0
    query_cache.misses = 0
    dev_cache.CACHE_ENABLED = False
    yield


@pytest.fixture(autouse=True)
def _clear_queue(redis_db):
    yield


def _fake_embed_batch(vector_by_text=None, *, fail_on=None):
    async def _fn(texts):
        if fail_on and any(t in fail_on for t in texts):
            raise gemini_client.EmbeddingProviderError("boom")
        if vector_by_text:
            return [vector_by_text.get(t, unit_vector(0)) for t in texts]
        return [unit_vector(0) for _ in texts]

    return _fn


# ---------------------------------------------------------------------------
# semantic_search: validation
# ---------------------------------------------------------------------------

async def test_query_below_min_len_raises(db_session: AsyncSession, redis_db, monkeypatch):
    with pytest.raises(VectorSearchQueryTooShortError):
        await service.semantic_search(
            db_session, user_id=1, raw_query=" a ", chat_id=None, limit=10, limits=_LIMITS
        )


async def test_blank_query_raises(db_session: AsyncSession, redis_db):
    with pytest.raises(VectorSearchQueryTooShortError):
        await service.semantic_search(
            db_session, user_id=1, raw_query="   ", chat_id=None, limit=10, limits=_LIMITS
        )


async def test_start_at_after_end_at_raises(db_session: AsyncSession, redis_db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    async def _fake_embed_query(t):
        return unit_vector(0)

    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _fake_embed_query)
    now = datetime.now(timezone.utc)
    with pytest.raises(VectorSearchQueryTooShortError):
        await service.semantic_search(
            db_session,
            user_id=1,
            raw_query="hello",
            chat_id=None,
            limit=10,
            limits=_LIMITS,
            start_at=now,
            end_at=now - timedelta(days=1),
        )


async def test_query_truncated_to_max_len(db_session: AsyncSession, redis_db, monkeypatch):
    seen = {}

    async def _fake_embed_query(text):
        seen["text"] = text
        return unit_vector(0)

    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _fake_embed_query)

    await _user(db_session, 921)
    await service.semantic_search(
        db_session, user_id=921, raw_query="x" * 100, chat_id=None, limit=10, limits=_LIMITS
    )
    assert len(seen["text"]) == _LIMITS.max_query_len


async def test_expanded_uses_wider_max_distance(db_session: AsyncSession, redis_db, monkeypatch):
    async def _embed_query(t):
        return unit_vector(0)

    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _embed_query)

    await _user(db_session, 922)
    await _chat(db_session, 9201, 922)
    await create_message(db_session, message_id=_mid(1), chat_id=9201, sender_id=922, content="far match")
    from modules.vector_search import crud as vector_crud

    await vector_crud.update_embeddings(db_session, [(_mid(1), unit_vector(1))])

    narrow = VectorSearchLimits(**{**_LIMITS.__dict__, "max_distance": 0.1, "max_distance_expanded": 1.9})

    resp = await service.semantic_search(
        db_session, user_id=922, raw_query="hi", chat_id=None, limit=10, limits=narrow, expanded=False
    )
    assert resp.results == []

    resp = await service.semantic_search(
        db_session, user_id=922, raw_query="hi", chat_id=None, limit=10, limits=narrow, expanded=True
    )
    assert [r.id for r in resp.results] == [str(_mid(1))]


async def test_dev_cache_used_when_enabled(db_session: AsyncSession, redis_db, monkeypatch, tmp_path):
    dev_cache.CACHE_ENABLED = True
    monkeypatch.setattr(dev_cache, "CACHE_PATH", str(tmp_path / "cache.sqlite"))

    calls = []

    async def _fake_embed_batch(texts):
        calls.append(list(texts))
        return [unit_vector(0) for _ in texts]

    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch)
    monkeypatch.setattr(query_cache.gemini_client, "embed_query", lambda t: (_ for _ in ()).throw(AssertionError("live cache should not be used")))

    await _user(db_session, 923)
    await service.semantic_search(
        db_session, user_id=923, raw_query="hello", chat_id=None, limit=10, limits=_LIMITS
    )
    assert calls == [["hello"]]


# ---------------------------------------------------------------------------
# flush_queue / flush_queue_if_pending
# ---------------------------------------------------------------------------

async def test_flush_queue_empty_returns_zero(db_session: AsyncSession, redis_db):
    assert await service.flush_queue(limits=_LIMITS) == 0


async def test_flush_queue_writes_embeddings_in_batches(db_session: AsyncSession, redis_db, monkeypatch):
    """flush_queue pops at most `queue_flush_size` (3) items in one call and
    embeds them via `gemini_batch_size` (2) chunks - draining a longer queue
    across multiple flush_queue calls is flush_queue_if_pending's job."""
    await _user(db_session, 924)
    await _chat(db_session, 9202, 924)
    mids = [_mid(10 + n) for n in range(5)]
    for i, mid in enumerate(mids):
        await create_message(db_session, message_id=mid, chat_id=9202, sender_id=924, content=f"m{i}")
        await queue.enqueue(mid, f"m{i}")

    batch_calls = []

    async def _fake_embed_batch(texts):
        batch_calls.append(len(texts))
        return [unit_vector(0) for _ in texts]

    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch)

    embedded = await service.flush_queue(limits=_LIMITS)
    assert embedded == 3  # queue_flush_size
    # gemini_batch_size=2 -> chunks of 2,1
    assert batch_calls == [2, 1]
    assert await queue.queue_length() == 2  # the remaining 2 of 5


async def test_flush_queue_drops_failed_batch_without_raising(db_session: AsyncSession, redis_db, monkeypatch):
    await _user(db_session, 925)
    await _chat(db_session, 9203, 925)
    mid = _mid(20)
    await create_message(db_session, message_id=mid, chat_id=9203, sender_id=925, content="doomed")
    await queue.enqueue(mid, "doomed")

    async def _fail(texts):
        raise gemini_client.EmbeddingProviderError("boom")

    monkeypatch.setattr(gemini_client, "embed_batch", _fail)

    embedded = await service.flush_queue(limits=_LIMITS)
    assert embedded == 0
    # Popped items are gone even though the embed failed - accepted gap (no requeue).
    assert await queue.queue_length() == 0


async def test_flush_queue_if_pending_drains_below_flush_size(db_session: AsyncSession, redis_db, monkeypatch):
    await _user(db_session, 926)
    await _chat(db_session, 9204, 926)
    mid = _mid(30)
    await create_message(db_session, message_id=mid, chat_id=9204, sender_id=926, content="one item")
    await queue.enqueue(mid, "one item")  # below queue_flush_size=3

    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch())

    await service.flush_queue_if_pending(limits=_LIMITS)
    assert await queue.queue_length() == 0


async def test_flush_queue_if_pending_stops_on_repeated_failure(db_session: AsyncSession, redis_db, monkeypatch):
    await _user(db_session, 927)
    await _chat(db_session, 9205, 927)
    mid = _mid(40)
    await create_message(db_session, message_id=mid, chat_id=9205, sender_id=927, content="bad")
    await queue.enqueue(mid, "bad")

    async def _fail(texts):
        raise gemini_client.EmbeddingProviderError("boom")

    monkeypatch.setattr(gemini_client, "embed_batch", _fail)

    # Must return (not hang) even though the queue is drained-but-unembedded.
    await asyncio.wait_for(service.flush_queue_if_pending(limits=_LIMITS), timeout=5)


async def test_search_flushes_pending_queue_first(db_session: AsyncSession, redis_db, monkeypatch):
    """A just-sent message still in the Redis queue (below auto-flush size)
    must be embedded and searchable within the same search call (ADR 0042
    point 3 - the on-demand trigger)."""
    await _user(db_session, 928)
    await _chat(db_session, 9206, 928)
    mid = _mid(50)
    await create_message(db_session, message_id=mid, chat_id=9206, sender_id=928, content="fresh")
    await queue.enqueue(mid, "fresh")

    async def _embed_query(t):
        return unit_vector(2)

    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch({"fresh": unit_vector(2)}))
    monkeypatch.setattr(query_cache.gemini_client, "embed_query", _embed_query)

    resp = await service.semantic_search(
        db_session, user_id=928, raw_query="fresh", chat_id=None, limit=10, limits=_LIMITS
    )
    assert [r.id for r in resp.results] == [str(mid)]


# ---------------------------------------------------------------------------
# enqueue_message_for_embedding
# ---------------------------------------------------------------------------

async def test_enqueue_skips_non_text_message_type(redis_db):
    await service.enqueue_message_for_embedding(_mid(60), "hello", message_type=5, limits=_LIMITS)
    assert await queue.queue_length() == 0


async def test_enqueue_skips_empty_content(redis_db):
    await service.enqueue_message_for_embedding(_mid(61), None, message_type=1, limits=_LIMITS)
    await service.enqueue_message_for_embedding(_mid(62), "", message_type=1, limits=_LIMITS)
    assert await queue.queue_length() == 0


async def test_enqueue_text_message_pushes_to_queue(redis_db):
    await service.enqueue_message_for_embedding(_mid(63), "hi", message_type=1, limits=_LIMITS)
    assert await queue.queue_length() == 1


async def test_enqueue_triggers_background_flush_at_threshold(db_session: AsyncSession, redis_db, monkeypatch):
    await _user(db_session, 929)
    await _chat(db_session, 9207, 929)
    mids = [_mid(70 + n) for n in range(3)]  # queue_flush_size=3
    for i, mid in enumerate(mids):
        await create_message(db_session, message_id=mid, chat_id=9207, sender_id=929, content=f"m{i}")

    monkeypatch.setattr(gemini_client, "embed_batch", _fake_embed_batch())

    for i, mid in enumerate(mids):
        await service.enqueue_message_for_embedding(mid, f"m{i}", message_type=1, limits=_LIMITS)

    # The flush is fired via asyncio.create_task - give the loop a turn.
    for _ in range(20):
        if await queue.queue_length() == 0:
            break
        await asyncio.sleep(0.05)
    assert await queue.queue_length() == 0
