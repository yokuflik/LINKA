import asyncio

import pytest

from services import realtime_service

pytestmark = pytest.mark.asyncio


async def _collect_one(chat_id: int, timeout: float = 2.0) -> dict:
    agen = realtime_service.subscribe_to_chat(chat_id)
    try:
        return await asyncio.wait_for(agen.__anext__(), timeout=timeout)
    finally:
        await agen.aclose()


async def test_subscriber_receives_a_published_event(redis_db):
    async def publish_soon():
        await asyncio.sleep(0.2)  # let the subscriber start listening first
        await realtime_service.publish_event(chat_id=1, event={"event": "new_message", "message_id": 42})

    received, _ = await asyncio.gather(_collect_one(chat_id=1), publish_soon())

    assert received == {"event": "new_message", "message_id": 42}


async def test_events_do_not_leak_across_chats(redis_db):
    async def publish_to_other_chat_then_the_target_chat():
        await asyncio.sleep(0.2)
        await realtime_service.publish_event(chat_id=999, event={"event": "new_message", "message_id": "wrong-chat"})
        await realtime_service.publish_event(chat_id=1, event={"event": "new_message", "message_id": "right-chat"})

    received, _ = await asyncio.gather(_collect_one(chat_id=1), publish_to_other_chat_then_the_target_chat())

    assert received["message_id"] == "right-chat"


async def test_multiple_subscribers_all_receive_the_same_event(redis_db):
    # Simulates several FastAPI instances, each with a locally-connected
    # member of the same chat, all subscribed to its fanout channel.
    async def publish_soon():
        await asyncio.sleep(0.2)
        await realtime_service.publish_event(chat_id=1, event={"event": "new_message", "message_id": 1})

    results = await asyncio.gather(
        _collect_one(chat_id=1),
        _collect_one(chat_id=1),
        _collect_one(chat_id=1),
        publish_soon(),
    )

    assert results[0] == results[1] == results[2] == {"event": "new_message", "message_id": 1}


async def test_publish_with_no_subscriber_does_not_raise(redis_db):
    # A chat with nobody currently connected anywhere is the common case -
    # publishing must be a no-op, not an error.
    await realtime_service.publish_event(chat_id=12345, event={"event": "new_message", "message_id": 1})


async def test_publish_handles_a_burst_of_events_in_order(redis_db):
    # A rapid-fire burst (e.g. someone spamming a group) must arrive in the
    # order it was sent, with none dropped.
    #
    # Note: an async generator's body (including the `pubsub.subscribe()`
    # call inside subscribe_to_chat) doesn't run at all until it's first
    # advanced - merely calling subscribe_to_chat() does nothing yet. So the
    # first __anext__() must be scheduled as its own task *before* publishing,
    # or every event below would be published before anyone actually
    # subscribed and would simply be lost (pub/sub has no replay/history).
    agen = realtime_service.subscribe_to_chat(chat_id=1)
    first_item_task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)  # let the subscribe() call actually land

        for i in range(100):
            await realtime_service.publish_event(chat_id=1, event={"seq": i})

        received = [await asyncio.wait_for(first_item_task, timeout=2.0)]
        received += [await asyncio.wait_for(agen.__anext__(), timeout=2.0) for _ in range(99)]
    finally:
        await agen.aclose()

    assert [e["seq"] for e in received] == list(range(100))
