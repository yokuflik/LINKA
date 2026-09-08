import asyncio

import pytest

from services import realtime_service
from services.fanout import routing

pytestmark = pytest.mark.asyncio

# FANOUT_REWRITE_PLAN.md step 3: publish_event no longer targets a per-chat
# channel. A process registers itself as serving a chat (routing.add_chat_for_
# instance) and listens on its own instance inbox; publish_event looks up the
# serving processes and publishes to each one's inbox.


async def _serve(server_id: str, chat_id: int) -> None:
    await routing.add_chat_for_instance(server_id, chat_id)


async def _collect_one(server_id: str, timeout: float = 2.0) -> dict:
    agen = realtime_service.subscribe_to_instance_inbox(server_id)
    try:
        return await asyncio.wait_for(agen.__anext__(), timeout=timeout)
    finally:
        await agen.aclose()


async def test_subscriber_receives_a_published_event(redis_db):
    await _serve("server-1", 1)

    async def publish_soon():
        await asyncio.sleep(0.2)
        await realtime_service.publish_event(chat_id=1, event={"event": "new_message", "message_id": 42})

    received, _ = await asyncio.gather(_collect_one("server-1"), publish_soon())

    assert received["event"] == "new_message"
    assert received["message_id"] == 42
    assert received["chat_id"] == "1"


async def test_events_do_not_leak_across_chats(redis_db):
    # server-1 serves chat 1 only; an event for chat 999 must not reach it.
    await _serve("server-1", 1)

    async def publish_other_then_target():
        await asyncio.sleep(0.2)
        await realtime_service.publish_event(chat_id=999, event={"message_id": "wrong-chat"})
        await realtime_service.publish_event(chat_id=1, event={"message_id": "right-chat"})

    received, _ = await asyncio.gather(_collect_one("server-1"), publish_other_then_target())

    assert received["message_id"] == "right-chat"


async def test_multiple_instances_serving_the_chat_all_receive_the_event(redis_db):
    await _serve("server-1", 1)
    await _serve("server-2", 1)
    await _serve("server-3", 1)

    async def publish_soon():
        await asyncio.sleep(0.2)
        await realtime_service.publish_event(chat_id=1, event={"message_id": 1})

    results = await asyncio.gather(
        _collect_one("server-1"),
        _collect_one("server-2"),
        _collect_one("server-3"),
        publish_soon(),
    )

    assert results[0]["message_id"] == results[1]["message_id"] == results[2]["message_id"] == 1


async def test_publish_with_no_serving_instance_does_not_raise(redis_db):
    await realtime_service.publish_event(chat_id=12345, event={"event": "new_message", "message_id": 1})


async def test_user_channel_delivers_to_its_own_subscriber_only(redis_db):
    agen_1 = realtime_service.subscribe_to_user(1)
    task_1 = asyncio.create_task(agen_1.__anext__())
    agen_2 = realtime_service.subscribe_to_user(2)
    task_2 = asyncio.create_task(agen_2.__anext__())
    try:
        await asyncio.sleep(0.2)
        await realtime_service.publish_user_event(1, {"event": "added_to_chat", "chat_id": "5"})

        event_1 = await asyncio.wait_for(task_1, timeout=2.0)
        assert event_1 == {"event": "added_to_chat", "chat_id": "5"}

        # User 2's channel saw nothing.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task_2), timeout=0.5)
    finally:
        task_2.cancel()
        try:
            await task_2
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        await agen_1.aclose()
        await agen_2.aclose()


async def test_presence_channel_delivers_to_the_target_channel(redis_db):
    agen = realtime_service.subscribe_to_presence(7)
    task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        await realtime_service.publish_presence_event(7, {"event": "presence_update", "user_id": "7", "online": True})
        event = await asyncio.wait_for(task, timeout=2.0)
    finally:
        await agen.aclose()

    assert event == {"event": "presence_update", "user_id": "7", "online": True}


async def test_publish_handles_a_burst_of_events_in_order(redis_db):
    await _serve("server-1", 1)

    agen = realtime_service.subscribe_to_instance_inbox("server-1")
    first_item_task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)

        for i in range(100):
            await realtime_service.publish_event(chat_id=1, event={"seq": i})

        received = [await asyncio.wait_for(first_item_task, timeout=2.0)]
        received += [await asyncio.wait_for(agen.__anext__(), timeout=2.0) for _ in range(99)]
    finally:
        await agen.aclose()

    assert [e["seq"] for e in received] == list(range(100))
