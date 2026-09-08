"""
Step 3: routing layer. A chat's events are published only to the inbox
channels of the processes that actually serve a local member of that chat -
not to every process subscribed to a per-chat channel.

Real Redis (no DB needed here). ``redis_db`` flushes between tests.
"""
import asyncio
import json

import pytest

from config import CHAT_INSTANCE_TTL_SECONDS
from services import realtime_service
from services.fanout import routing
from services.redis_client import redis_client

pytestmark = pytest.mark.asyncio


async def _collect_one(server_id: str, out: list) -> None:
    agen = realtime_service.subscribe_to_instance_inbox(server_id)
    try:
        async for event in agen:
            out.append(event)
            return
    finally:
        await agen.aclose()


async def test_event_reaches_only_instances_serving_the_chat(redis_db):
    chat_id = 1001
    await routing.add_chat_for_instance("server-A", chat_id)
    # server-B serves a different chat only
    await routing.add_chat_for_instance("server-B", 2002)

    got_a, got_b = [], []
    task_a = asyncio.create_task(_collect_one("server-A", got_a))
    task_b = asyncio.create_task(_collect_one("server-B", got_b))
    await asyncio.sleep(0.2)

    await realtime_service.publish_event(chat_id, {"event": "new_message", "message_id": "42"})

    await asyncio.wait_for(task_a, timeout=2.0)
    task_b.cancel()

    assert got_a and got_a[0]["message_id"] == "42"
    assert got_a[0]["chat_id"] == str(chat_id)
    assert got_b == []


async def test_instances_for_chat_reflects_add_and_remove(redis_db):
    chat_id = 3003
    await routing.add_chat_for_instance("server-A", chat_id)
    await routing.add_chat_for_instance("server-B", chat_id)
    assert await routing.instances_for_chat(chat_id) == {"server-A", "server-B"}

    await routing.remove_chat_for_instance("server-A", chat_id)
    assert await routing.instances_for_chat(chat_id) == {"server-B"}


async def test_expired_registration_is_not_delivered_to(redis_db, monkeypatch):
    chat_id = 4004
    # Force a tiny TTL so the entry expires within the test.
    monkeypatch.setattr(routing, "CHAT_INSTANCE_TTL_SECONDS", 1)
    await routing.add_chat_for_instance("server-dead", chat_id)
    assert await routing.instances_for_chat(chat_id) == {"server-dead"}

    await asyncio.sleep(1.2)
    assert await routing.instances_for_chat(chat_id) == set()

    # publish_event to a chat nobody serves is a harmless no-op
    await realtime_service.publish_event(chat_id, {"event": "new_message"})


async def test_heartbeat_refreshes_ttl(redis_db, monkeypatch):
    chat_id = 5005
    monkeypatch.setattr(routing, "CHAT_INSTANCE_TTL_SECONDS", 2)
    await routing.add_chat_for_instance("server-A", chat_id)

    await asyncio.sleep(1.0)
    await routing.heartbeat("server-A")
    await asyncio.sleep(1.5)  # past the original TTL, within the refreshed one

    assert await routing.instances_for_chat(chat_id) == {"server-A"}


async def test_unregister_instance_clears_all_its_chats(redis_db):
    await routing.register_instance_for_chats("server-A", [10, 11, 12])
    assert await routing.instances_for_chat(11) == {"server-A"}

    await routing.unregister_instance("server-A")

    for chat_id in (10, 11, 12):
        assert await routing.instances_for_chat(chat_id) == set()
    assert not await redis_client.exists("instance_chats:server-A")


async def test_publish_event_fans_to_multiple_serving_instances(redis_db):
    chat_id = 6006
    await routing.add_chat_for_instance("server-A", chat_id)
    await routing.add_chat_for_instance("server-B", chat_id)

    got_a, got_b = [], []
    ta = asyncio.create_task(_collect_one("server-A", got_a))
    tb = asyncio.create_task(_collect_one("server-B", got_b))
    await asyncio.sleep(0.2)

    await realtime_service.publish_event(chat_id, {"event": "typing", "user_id": "9"})

    await asyncio.wait_for(asyncio.gather(ta, tb), timeout=2.0)
    assert got_a[0]["event"] == "typing"
    assert got_b[0]["event"] == "typing"
