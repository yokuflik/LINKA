"""
Background consumer that drains ``message_send_stream``: for each queued
outgoing message it runs the full send flow
(``message_service.process_outgoing`` - persist + fan-out) and acks.

Started once per app process from main.py's lifespan. The send stream is
sharded by chat_id (SEND_STREAM_SHARDS) so a chat's messages stay ordered on
one shard; run_forever spins one consumer task per shard, and every process
shares one consumer group per shard, so adding replicas splits the load.

Failure handling per entry (no batch collapsing - every message is distinct):
  - permanent (bad media, not a participant, too long): emit
    ``message_failed`` on the sender's user channel, then ACK - retrying
    would never succeed.
  - already sent (duplicate stream entry): emit ``message_already_sent``,
    then ACK.
  - transient (DB blip, Redis hiccup): do NOT ACK - XAUTOCLAIM hands the
    entry to another worker after SEND_STREAM_CLAIM_IDLE_MS.
"""
import asyncio
import logging
from typing import Optional

from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    MESSAGE_SEND_STREAM_GROUP,
    MESSAGE_SEND_STREAM_KEY,
    SEND_STREAM_CLAIM_IDLE_MS,
    SEND_STREAM_SHARDS,
    SEND_WORKER_BATCH,
    SEND_WORKER_BLOCK_MS,
    SERVER_ID,
)
from database.connection import session_scope
from services import message_service, realtime_service
from services.fanout import send_queue
from services.redis_client import redis_client
from services.storage.errors import MediaNotFoundError, MediaValidationError

logger = logging.getLogger(__name__)

_CONSUMER_NAME = f"send-worker-{SERVER_ID}"

# process_outgoing failures that will never succeed on a retry.
_PERMANENT_ERRORS = (
    MediaValidationError,
    MediaNotFoundError,
    message_service.NotAParticipantError,
    message_service.MessageTooLongError,
)


def _int_or_none(value: str) -> Optional[int]:
    return int(value) if value else None


def _str_or_none(value: str) -> Optional[str]:
    return value if value else None


def _rebuild_media(fields: dict) -> Optional[dict]:
    key = _str_or_none(fields.get("media_key", ""))
    if key is None:
        return None
    media: dict = {"key": key}
    name = _str_or_none(fields.get("media_name", ""))
    if name is not None:
        media["name"] = name
    duration = _int_or_none(fields.get("media_duration_seconds", ""))
    if duration is not None:
        media["duration_seconds"] = duration
    return media


async def process_entry(session: AsyncSession, fields: dict) -> None:
    """
    Run one queued message through the send flow. Raises on a transient
    failure (caller must not ack); handles permanent / already-sent cases
    internally by notifying the sender.
    """
    chat_id = int(fields["chat_id"])
    sender_id = int(fields["sender_id"])
    client_message_id = fields["client_message_id"]

    try:
        await message_service.process_outgoing(
            session,
            sender_id=sender_id,
            chat_id=chat_id,
            client_message_id=client_message_id,
            content=_str_or_none(fields.get("content", "")),
            type=int(fields.get("type") or 1),
            reply_to_message_id=_int_or_none(fields.get("reply_to_message_id", "")),
            media=_rebuild_media(fields),
        )
    except message_service.MessageAlreadySentError as exc:
        # The message was persisted by an earlier stream entry. If that entry
        # crashed between the DB commit and enqueuing fan-out, the message
        # exists but was never delivered - re-enqueue fan-out (idempotent,
        # clients dedupe by message_id) so it can't be lost.
        await send_queue.enqueue_fanout(
            message_id=exc.message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            client_message_id=client_message_id,
        )
        await realtime_service.publish_user_event(
            sender_id,
            {
                "event": "message_already_sent",
                "chat_id": str(chat_id),
                "client_message_id": client_message_id,
                "message_id": str(exc.message_id),
            },
        )
    except _PERMANENT_ERRORS as exc:
        logger.info(
            "send worker: dropping message (permanent failure) chat=%s sender=%s cmid=%s: %s",
            chat_id, sender_id, client_message_id, exc,
        )
        await realtime_service.publish_user_event(
            sender_id,
            {
                "event": "message_failed",
                "chat_id": str(chat_id),
                "client_message_id": client_message_id,
                "reason": str(exc),
            },
        )


async def _claim_stale(stream_key: str, count: int) -> list:
    try:
        result = await redis_client.xautoclaim(
            stream_key,
            MESSAGE_SEND_STREAM_GROUP,
            _CONSUMER_NAME,
            min_idle_time=SEND_STREAM_CLAIM_IDLE_MS,
            count=count,
        )
    except ResponseError as exc:
        logger.warning("XAUTOCLAIM unavailable, skipping stale-entry reclaim: %s", exc)
        return []
    claimed = result[1] if len(result) >= 2 else []
    return [(entry_id, fields) for entry_id, fields in claimed if fields]


async def _drain_shard(
    session: AsyncSession,
    stream_key: str,
    *,
    block_ms: int,
    count: int,
    claim_stale: bool,
) -> int:
    response = await redis_client.xreadgroup(
        MESSAGE_SEND_STREAM_GROUP,
        _CONSUMER_NAME,
        {stream_key: ">"},
        count=count,
        block=block_ms or None,
    )
    entries: list = []
    if response:
        entries = list(response[0][1])

    if claim_stale and len(entries) < count:
        entries.extend(await _claim_stale(stream_key, count - len(entries)))

    if not entries:
        return 0

    ack_ids: list = []
    for entry_id, fields in entries:
        try:
            await process_entry(session, fields)
            # process_outgoing / the notify paths don't commit themselves -
            # commit per entry so one transient failure can't roll back
            # messages already persisted in this batch.
            await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Transient - roll back this entry, leave it unacked for
            # XAUTOCLAIM to hand to another worker.
            logger.exception("send worker: entry %s failed transiently, will be reclaimed", entry_id)
            await session.rollback()
            continue
        ack_ids.append(entry_id)

    if ack_ids:
        await redis_client.xack(stream_key, MESSAGE_SEND_STREAM_GROUP, *ack_ids)
    return len(ack_ids)


async def drain_once(
    session: AsyncSession,
    *,
    block_ms: int = 0,
    count: Optional[int] = None,
    claim_stale: bool = True,
    shard: Optional[int] = None,
) -> int:
    """
    Read one batch and process it. With ``shard`` given, only that shard's
    stream is drained (the per-shard worker loop passes it); with ``shard``
    None, every shard is drained in turn (tests, and a single-shard config).
    Returns how many entries were acked.
    """
    count = count or SEND_WORKER_BATCH

    # Cheap and idempotent - keeps drain_once usable on its own (tests, a
    # fresh Redis) without a separate ensure_group() call first.
    await send_queue.ensure_group()

    if shard is not None:
        key = send_queue.stream_key(MESSAGE_SEND_STREAM_KEY, shard)
        return await _drain_shard(
            session, key, block_ms=block_ms, count=count, claim_stale=claim_stale
        )

    total = 0
    for key in send_queue.send_stream_keys():
        total += await _drain_shard(
            session, key, block_ms=block_ms, count=count, claim_stale=claim_stale
        )
    return total


async def _run_shard(shard: int, stop_event: asyncio.Event | None) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            async with session_scope() as session:
                acked = await drain_once(session, block_ms=SEND_WORKER_BLOCK_MS, shard=shard)
            if acked == 0:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("send worker (shard %s): drain iteration failed", shard)
            await asyncio.sleep(1.0)


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    try:
        await send_queue.ensure_group()
    except Exception:  # Redis down at boot - the loop below keeps retrying
        logger.exception("send worker: ensure_group failed, will retry")

    tasks = [
        asyncio.create_task(_run_shard(s, stop_event)) for s in range(SEND_STREAM_SHARDS)
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        raise
