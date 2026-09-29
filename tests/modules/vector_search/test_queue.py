"""Redis-list coverage for the vector-embed flush queue (ADR 0042): plain
enqueue/pop_batch/queue_length against a real Redis, not mocked.
"""
import pytest

from modules.vector_search import queue

pytestmark = pytest.mark.asyncio


async def test_enqueue_then_length(redis_db):
    assert await queue.queue_length() == 0
    await queue.enqueue(1, "a")
    await queue.enqueue(2, "b")
    assert await queue.queue_length() == 2


async def test_pop_batch_returns_oldest_first_and_removes_them(redis_db):
    await queue.enqueue(1, "a")
    await queue.enqueue(2, "b")
    await queue.enqueue(3, "c")

    popped = await queue.pop_batch(2)
    assert [p["id"] for p in popped] == ["1", "2"]
    assert [p["content"] for p in popped] == ["a", "b"]
    assert await queue.queue_length() == 1


async def test_pop_batch_on_empty_queue_returns_empty_list(redis_db):
    assert await queue.pop_batch(10) == []


async def test_pop_batch_more_than_available_drains_queue(redis_db):
    await queue.enqueue(1, "a")
    popped = await queue.pop_batch(100)
    assert [p["id"] for p in popped] == ["1"]
    assert await queue.queue_length() == 0


async def test_enqueue_returns_length_after_push(redis_db):
    assert await queue.enqueue(1, "a") == 1
    assert await queue.enqueue(2, "b") == 2
