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

The generic stream-consuming machinery lives in base_worker.BaseStreamConsumer;
this module only holds the send-specific business logic.
"""
import asyncio
import logging
from typing import Optional

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
from services import message_service, realtime_service
from services.fanout import send_queue
from services.fanout.base_worker import BaseStreamConsumer
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


class SendWorker(BaseStreamConsumer):
    name = "send worker"
    consumer_name = _CONSUMER_NAME
    group = MESSAGE_SEND_STREAM_GROUP
    shard_count = SEND_STREAM_SHARDS
    default_batch = SEND_WORKER_BATCH
    block_ms = SEND_WORKER_BLOCK_MS
    claim_idle_ms = SEND_STREAM_CLAIM_IDLE_MS

    async def ensure_group(self) -> None:
        await send_queue.ensure_group()

    def stream_keys(self) -> list:
        return send_queue.send_stream_keys()

    def stream_key_for_shard(self, shard: int) -> str:
        return send_queue.stream_key(MESSAGE_SEND_STREAM_KEY, shard)

    async def after_entry(self, session: AsyncSession) -> None:
        # process_outgoing / the notify paths don't commit themselves -
        # commit per entry so one transient failure can't roll back
        # messages already persisted in this batch.
        await session.commit()

    async def process_entry(self, session: AsyncSession, fields: dict) -> None:
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


_worker = SendWorker()


async def process_entry(session: AsyncSession, fields: dict) -> None:
    await _worker.process_entry(session, fields)


async def drain_once(
    session: AsyncSession,
    *,
    block_ms: int = 0,
    count: Optional[int] = None,
    claim_stale: bool = True,
    shard: Optional[int] = None,
) -> int:
    return await _worker.drain_once(
        session, block_ms=block_ms, count=count, claim_stale=claim_stale, shard=shard
    )


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    await _worker.run_forever(stop_event)
