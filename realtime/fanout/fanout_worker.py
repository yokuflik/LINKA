"""
Background consumer that drains ``message_fanout_stream``: for each queued
reference it loads the persisted message and runs the fan-out
(``message_service.fan_out_message`` - build the new_message event, publish
it to the chat channel, push to offline members), then acks.

Second hop after services/fanout/worker.py: the send worker persists the
message and enqueues a reference here. Two streams because the DB write and
the Redis/network fan-out fail and scale differently.

Started once per app process from main.py's lifespan. Sharded by chat_id
(FANOUT_STREAM_SHARDS), one consumer task per shard, one shared consumer group
per shard - adding replicas splits the load.

Failure handling per entry:
  - message row not found (retention pruned it, or a spurious entry): ACK -
    there's nothing to fan out and retrying won't change that.
  - transient (Redis hiccup, DB blip): do NOT ACK - XAUTOCLAIM hands the
    entry to another worker after FANOUT_STREAM_CLAIM_IDLE_MS. A redelivered
    fan-out just re-publishes; clients dedupe by message_id.

The generic stream-consuming machinery lives in base_worker.BaseStreamConsumer;
this module only holds the fan-out-specific business logic.
"""
import asyncio
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    FANOUT_STREAM_CLAIM_IDLE_MS,
    FANOUT_STREAM_SHARDS,
    FANOUT_WORKER_BATCH,
    FANOUT_WORKER_BLOCK_MS,
    MESSAGE_FANOUT_STREAM_GROUP,
    MESSAGE_FANOUT_STREAM_KEY,
    SERVER_ID,
)
from database.crud.crud_message import get_message_by_id
from services import message_service
from services.fanout import send_queue
from services.fanout.base_worker import BaseStreamConsumer

logger = logging.getLogger(__name__)

_CONSUMER_NAME = f"fanout-worker-{SERVER_ID}"


def _str_or_none(value: str) -> Optional[str]:
    return value if value else None


class FanoutWorker(BaseStreamConsumer):
    name = "fanout worker"
    consumer_name = _CONSUMER_NAME
    group = MESSAGE_FANOUT_STREAM_GROUP
    shard_count = FANOUT_STREAM_SHARDS
    default_batch = FANOUT_WORKER_BATCH
    block_ms = FANOUT_WORKER_BLOCK_MS
    claim_idle_ms = FANOUT_STREAM_CLAIM_IDLE_MS

    async def ensure_group(self) -> None:
        await send_queue.ensure_fanout_group()

    def stream_keys(self) -> list:
        return send_queue.fanout_stream_keys()

    def stream_key_for_shard(self, shard: int) -> str:
        return send_queue.stream_key(MESSAGE_FANOUT_STREAM_KEY, shard)

    async def process_entry(self, session: AsyncSession, fields: dict) -> None:
        """
        Fan out one persisted message. Raises on a transient failure (caller must
        not ack); a missing message row is treated as done (nothing to fan out).
        """
        message_id = int(fields["message_id"])
        chat_id = int(fields["chat_id"])
        client_message_id = _str_or_none(fields.get("client_message_id", ""))

        message = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
        if message is None:
            logger.info(
                "fanout worker: message %s in chat %s not found, nothing to fan out", message_id, chat_id
            )
            return

        await message_service.fan_out_message(session, message, client_message_id=client_message_id)


_worker = FanoutWorker()


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
