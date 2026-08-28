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
"""
import asyncio
import logging
from typing import Optional

from redis.exceptions import ResponseError
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
from database.connection import session_scope
from database.crud.crud_message import get_message_by_id
from services import message_service
from services.fanout import send_queue
from services.redis_client import redis_client

logger = logging.getLogger(__name__)

_CONSUMER_NAME = f"fanout-worker-{SERVER_ID}"


def _str_or_none(value: str) -> Optional[str]:
    return value if value else None


async def process_entry(session: AsyncSession, fields: dict) -> None:
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


async def _claim_stale(stream_key: str, count: int) -> list:
    try:
        result = await redis_client.xautoclaim(
            stream_key,
            MESSAGE_FANOUT_STREAM_GROUP,
            _CONSUMER_NAME,
            min_idle_time=FANOUT_STREAM_CLAIM_IDLE_MS,
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
        MESSAGE_FANOUT_STREAM_GROUP,
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
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "fanout worker: entry %s failed transiently, will be reclaimed", entry_id
            )
            continue
        ack_ids.append(entry_id)

    if ack_ids:
        await redis_client.xack(stream_key, MESSAGE_FANOUT_STREAM_GROUP, *ack_ids)
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
    Read one batch and fan out each entry. With ``shard`` given, only that
    shard's stream is drained; with ``shard`` None, every shard is drained in
    turn (tests, single-shard config). Returns how many entries were acked.
    """
    count = count or FANOUT_WORKER_BATCH

    await send_queue.ensure_fanout_group()

    if shard is not None:
        key = send_queue.stream_key(MESSAGE_FANOUT_STREAM_KEY, shard)
        return await _drain_shard(
            session, key, block_ms=block_ms, count=count, claim_stale=claim_stale
        )

    total = 0
    for key in send_queue.fanout_stream_keys():
        total += await _drain_shard(
            session, key, block_ms=block_ms, count=count, claim_stale=claim_stale
        )
    return total


async def _run_shard(shard: int, stop_event: asyncio.Event | None) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            async with session_scope() as session:
                acked = await drain_once(session, block_ms=FANOUT_WORKER_BLOCK_MS, shard=shard)
            if acked == 0:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("fanout worker (shard %s): drain iteration failed", shard)
            await asyncio.sleep(1.0)


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    try:
        await send_queue.ensure_fanout_group()
    except Exception:  # Redis down at boot - the loop below keeps retrying
        logger.exception("fanout worker: ensure_fanout_group failed, will retry")

    tasks = [
        asyncio.create_task(_run_shard(s, stop_event)) for s in range(FANOUT_STREAM_SHARDS)
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
