"""
Routing table for chat fan-out (FANOUT_REWRITE_PLAN.md step 3).

Problem this solves: with per-chat Redis pub/sub, *every* process subscribed to
a chat's channel receives every event for that chat and filters locally - a
large group with members spread across many processes turns one message into
one delivery per process regardless of whether that process has a recipient.

Model: each process registers, in Redis, the chats it currently serves
(has at least one local WebSocket member of). The fan-out worker looks up
which processes serve a chat and publishes the event only to those processes'
personal inbox channels (see realtime_service.publish_to_instance).

Keys:
  - chat_instances:{chat_id}   SET of server_ids serving that chat. TTL'd;
                               refreshed by heartbeat() so a crashed process's
                               membership expires.
  - instance_chats:{server_id} SET of chat_ids this process serves. Used to
                               refresh the TTLs and to clean up on shutdown.

All operations are best-effort: a Redis blip that drops a registration just
means a missed live event until the next heartbeat re-adds it (the message is
still persisted; clients reconcile on reconnect / history fetch).
"""
import logging

from config import CHAT_INSTANCE_TTL_SECONDS
from services.redis_client import redis_client

logger = logging.getLogger(__name__)

_CHAT_INSTANCES_PREFIX = "chat_instances:"
_INSTANCE_CHATS_PREFIX = "instance_chats:"


def _chat_key(chat_id: int) -> str:
    return f"{_CHAT_INSTANCES_PREFIX}{chat_id}"


def _instance_key(server_id: str) -> str:
    return f"{_INSTANCE_CHATS_PREFIX}{server_id}"


async def register_instance_for_chats(server_id: str, chat_ids: list[int]) -> None:
    """Add this process to each chat's serving set and record the reverse map."""
    if not chat_ids:
        return
    pipe = redis_client.pipeline()
    for chat_id in chat_ids:
        pipe.sadd(_chat_key(chat_id), server_id)
        pipe.expire(_chat_key(chat_id), CHAT_INSTANCE_TTL_SECONDS)
    pipe.sadd(_instance_key(server_id), *[str(c) for c in chat_ids])
    await pipe.execute()


async def add_chat_for_instance(server_id: str, chat_id: int) -> None:
    """Dynamic subscribe: this process gained its first local member of chat_id."""
    pipe = redis_client.pipeline()
    pipe.sadd(_chat_key(chat_id), server_id)
    pipe.expire(_chat_key(chat_id), CHAT_INSTANCE_TTL_SECONDS)
    pipe.sadd(_instance_key(server_id), str(chat_id))
    await pipe.execute()


async def remove_chat_for_instance(server_id: str, chat_id: int) -> None:
    """Dynamic unsubscribe: this process lost its last local member of chat_id."""
    pipe = redis_client.pipeline()
    pipe.srem(_chat_key(chat_id), server_id)
    pipe.srem(_instance_key(server_id), str(chat_id))
    await pipe.execute()


async def instances_for_chat(chat_id: int) -> set[str]:
    members = await redis_client.smembers(_chat_key(chat_id))
    return {m.decode() if isinstance(m, bytes) else m for m in members}


async def heartbeat(server_id: str) -> None:
    """
    Re-assert this process's chat registrations and refresh their TTLs. Runs
    every ROUTING_HEARTBEAT_INTERVAL_SECONDS from the lifespan - the interval
    must stay well below CHAT_INSTANCE_TTL_SECONDS.
    """
    raw = await redis_client.smembers(_instance_key(server_id))
    chat_ids = [int(m.decode() if isinstance(m, bytes) else m) for m in raw]
    if not chat_ids:
        # Keep the reverse-map key itself alive so a process serving zero chats
        # (all clients disconnected) still cleans up properly on shutdown.
        await redis_client.expire(_instance_key(server_id), CHAT_INSTANCE_TTL_SECONDS)
        return
    pipe = redis_client.pipeline()
    for chat_id in chat_ids:
        pipe.sadd(_chat_key(chat_id), server_id)
        pipe.expire(_chat_key(chat_id), CHAT_INSTANCE_TTL_SECONDS)
    pipe.expire(_instance_key(server_id), CHAT_INSTANCE_TTL_SECONDS)
    await pipe.execute()


async def unregister_instance(server_id: str) -> None:
    """On graceful shutdown: drop this process from every chat it served."""
    raw = await redis_client.smembers(_instance_key(server_id))
    chat_ids = [int(m.decode() if isinstance(m, bytes) else m) for m in raw]
    pipe = redis_client.pipeline()
    for chat_id in chat_ids:
        pipe.srem(_chat_key(chat_id), server_id)
    pipe.delete(_instance_key(server_id))
    await pipe.execute()
